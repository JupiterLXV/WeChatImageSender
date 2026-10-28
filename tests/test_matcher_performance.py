"""Performance-path safety using synthetic pixels, never a desktop window."""
import gc
import unittest
import weakref

from PIL import Image, ImageDraw

from wechat_sender.image_utils import VisualBubbleVerifier


class TestMatcherPerformance(unittest.TestCase):
    def source(self):
        image = Image.new("RGB", (160, 120), (35, 110, 205))
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 15, 65, 70), fill=(220, 80, 40))
        draw.ellipse((100, 45, 145, 100), fill=(235, 220, 105))
        draw.line((10, 110, 145, 10), fill=(235, 245, 250), width=4)
        # The existing center-overlay detector deliberately excludes source
        # edges. Keep this ring fixture's center flat, as its original tests do.
        draw.rectangle((50, 30, 110, 90), fill=(35, 110, 205))
        return image

    def scene(self, source, *, top=None, bottom=520, indicator=None, angle=0, scale=1.0):
        frame = Image.new("RGB", (round(640*scale), round(700*scale)), (28, 28, 28))
        thumbnail = source.resize((round(160*scale), round(120*scale)))
        x = round(450*scale)
        if top is not None:
            frame.paste(thumbnail, (x, round(top*scale)))
        if bottom is not None:
            y = round(bottom*scale)
            frame.paste(thumbnail, (x, y))
            if indicator:
                cx = x + thumbnail.width//2 if indicator.startswith("center") else x-round(25*scale)
                cy, radius = y + thumbnail.height//2, round(9*scale)
                box = (cx-radius, cy-radius, cx+radius, cy+radius)
                draw = ImageDraw.Draw(frame)
                if indicator.endswith("animated"):
                    draw.arc(box, angle, angle+160, fill=(210, 210, 210), width=max(2, round(3*scale)))
                else:
                    draw.ellipse(box, outline=(210, 210, 210), width=max(2, round(3*scale)))
        return frame

    def test_identical_frames_use_local_matching_with_the_same_result(self):
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        first = verifier.evaluate(frame, source, frame.copy())
        repeated = verifier.evaluate(frame.copy(), source, frame.copy(), [first.bbox[2]])
        self.assertTrue(repeated.is_upload_complete)
        self.assertEqual(repeated.bbox, first.bbox)
        self.assertAlmostEqual(repeated.similarity, first.similarity, places=6)
        self.assertEqual(verifier.performance_counters["full_searches"], 1)
        self.assertEqual(verifier.performance_counters["cache_hits"], 1)

    def test_frozen_side_and_center_indicators_are_rechecked_on_cache_hits(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            for indicator in ("side", "center"):
                with self.subTest(scale=scale, indicator=indicator):
                    source = self.source()
                    frame = self.scene(source, indicator=indicator, scale=scale)
                    verifier = VisualBubbleVerifier()
                    first = verifier.evaluate(frame, source, frame.copy())
                    self.assertTrue(first.has_static_spinner)
                    status = verifier.evaluate(frame.copy(), source, frame.copy(), [first.bbox[2]])
                    self.assertGreater(verifier.performance_counters["cache_hits"], 0)
                    self.assertTrue(status.has_static_spinner)
                    self.assertFalse(status.is_upload_complete)

    def test_side_and_center_animation_still_block_completion_on_local_path(self):
        for indicator in ("side-animated", "center-animated"):
            with self.subTest(indicator=indicator):
                source = self.source()
                before = self.scene(source, indicator=indicator, angle=10)
                after = self.scene(source, indicator=indicator, angle=85)
                verifier = VisualBubbleVerifier()
                first = verifier.evaluate(before, source, before.copy())
                status = verifier.evaluate(after, source, before, [first.bbox[2]])
                self.assertGreater(verifier.performance_counters["cache_hits"], 0)
                self.assertFalse(status.is_upload_complete)

    def test_new_bottom_duplicate_invalidates_local_location_before_checking_its_spinner(self):
        source = self.source()
        before = self.scene(source, top=50, bottom=None)
        after = self.scene(source, top=50, indicator="side")
        verifier = VisualBubbleVerifier()
        old = verifier.evaluate(before, source, before.copy())
        latest = verifier.evaluate(after, source, before, [old.bbox[2]])
        self.assertEqual(verifier.performance_counters["full_searches"], 2)
        self.assertGreater(latest.bbox[1], 500)
        self.assertTrue(latest.has_static_spinner)
        self.assertFalse(latest.is_upload_complete)

    def test_moving_photo_forces_full_relocation(self):
        source = self.source()
        before, after = self.scene(source), self.scene(source, bottom=420)
        verifier = VisualBubbleVerifier()
        first = verifier.evaluate(before, source, before.copy())
        moved = verifier.evaluate(after, source, before, [first.bbox[2]])
        self.assertEqual(verifier.performance_counters["full_searches"], 2)
        self.assertLess(moved.bbox[1], first.bbox[1]-50)

    def test_frame_size_change_cannot_reuse_the_old_location(self):
        source = self.source()
        before, after = self.scene(source), self.scene(source, scale=1.5)
        verifier = VisualBubbleVerifier()
        verifier.evaluate(before, source, before.copy())
        scaled = verifier.evaluate(after, source, after.copy())
        self.assertEqual(verifier.performance_counters["full_searches"], 2)
        self.assertLessEqual(abs(scaled.bbox[2]-240), 10)

    def test_source_content_mutation_invalidates_arrays_and_templates(self):
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        first = verifier.evaluate(frame, source, frame.copy())
        key = verifier._source_key
        source.paste((220, 30, 170), (0, 0, 160, 120))
        verifier.evaluate(frame, source, frame.copy(), [first.bbox[2]])
        self.assertNotEqual(verifier._source_key, key)
        self.assertEqual(verifier.performance_counters["source_cache_misses"], 2)
        self.assertEqual(verifier.performance_counters["full_searches"], 2)

    def test_indexed_image_palette_mutation_invalidates_templates(self):
        source = self.source().quantize(colors=8)
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        verifier.locate_thumbnail(frame, source, [160])
        key = verifier._source_key
        palette = source.getpalette()
        palette[:3] = [(component + 101) % 256 for component in palette[:3]]
        source.putpalette(palette)
        verifier.locate_thumbnail(frame, source, [160])
        self.assertNotEqual(verifier._source_key, key)
        self.assertEqual(verifier.performance_counters["source_cache_misses"], 2)

    def test_red_failure_badge_is_rechecked_on_local_path(self):
        source = self.source()
        before = self.scene(source)
        after = before.copy()
        ImageDraw.Draw(after).ellipse((416, 571, 434, 589), fill=(230, 30, 30))
        verifier = VisualBubbleVerifier()
        first = verifier.evaluate(before, source, before.copy())
        failed = verifier.evaluate(after, source, before, [first.bbox[2]])
        self.assertGreater(verifier.performance_counters["cache_hits"], 0)
        self.assertTrue(failed.has_failure_badge)
        self.assertFalse(failed.is_upload_complete)

    def test_center_overlay_cannot_drift_a_local_match(self):
        source = self.source()
        before = self.scene(source)
        after = self.scene(source, indicator="center")
        verifier = VisualBubbleVerifier()
        first = verifier.evaluate(before, source, before.copy())
        overlay = verifier.evaluate(after, source, before, [first.bbox[2]])
        reference = VisualBubbleVerifier().evaluate(after, source, before, [first.bbox[2]])
        self.assertEqual(overlay.bbox, reference.bbox)
        self.assertAlmostEqual(overlay.similarity, reference.similarity, places=6)
        self.assertFalse(overlay.is_upload_complete)

    def test_large_prepared_source_releases_scope_and_bounds_arrays(self):
        source = self.source().resize((3000, 2000))
        reference = weakref.ref(source)
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        verifier.evaluate(frame, source, frame.copy(), [160])
        self.assertIsNone(verifier._source_arrays)
        verifier.end_image()
        self.assertIsNone(verifier._prepared_source_ref)
        self.assertIsNone(verifier._source_scope)
        self.assertLessEqual(verifier._template_bytes, 16*1024*1024)
        del source
        gc.collect()
        self.assertIsNone(reference())

    def test_cache_does_not_keep_a_pil_source_alive(self):
        source = self.source()
        reference = weakref.ref(source)
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        verifier.evaluate(frame, source, frame.copy())
        del source
        gc.collect()
        self.assertIsNone(reference())
        self.assertIsNone(verifier._source_scope)

    def test_template_cache_is_bounded_for_many_sizes(self):
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        verifier.locate_thumbnail(frame, source, list(range(60, 360, 4)))
        self.assertLessEqual(len(verifier._templates), 64)
        self.assertLessEqual(verifier._template_bytes, 16*1024*1024)

    def test_repeated_reference_width_does_not_resize_templates_again(self):
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        first = verifier.locate_thumbnail(frame, source, [160])
        resizes = verifier.performance_counters["template_resizes"]
        second = verifier.locate_thumbnail(frame, source.copy(), [160])
        self.assertEqual(second, first)
        self.assertEqual(verifier.performance_counters["template_resizes"], resizes)
        self.assertGreater(verifier.performance_counters["template_cache_hits"], 0)

    def test_prepared_immutable_image_avoids_rehashing_but_direct_calls_still_validate(self):
        from unittest.mock import patch
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        first = verifier.evaluate(frame, source, frame.copy())
        with patch.object(source, "tobytes", side_effect=AssertionError("already prepared")):
            status = verifier.evaluate(frame, source, frame.copy(), [first.bbox[2]])
        self.assertTrue(status.is_upload_complete)
        verifier.end_image()
        self.assertIsNone(verifier._prepared_source_ref)
        source.paste((10, 220, 45), (0, 0, 160, 120))
        verifier.locate_thumbnail(frame, source, [160])
        self.assertEqual(verifier.performance_counters["source_cache_misses"], 2)


if __name__ == "__main__":
    unittest.main()
