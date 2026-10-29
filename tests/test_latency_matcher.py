"""Regression cases for a changing background and a large immutable source."""
import gc
import unittest
import weakref
from unittest.mock import patch

from PIL import Image, ImageDraw

from wechat_sender.image_utils import VisualBubbleVerifier


class TestLargeSourceLatency(unittest.TestCase):
    @staticmethod
    def source(size=(3000, 2000)):
        image = Image.new("RGB", (160, 120), (35, 110, 205))
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 15, 65, 70), fill=(220, 80, 40))
        draw.ellipse((100, 45, 145, 100), fill=(235, 220, 105))
        draw.line((10, 110, 145, 10), fill=(235, 245, 250), width=4)
        draw.rectangle((50, 30, 110, 90), fill=(35, 110, 205))
        return image.resize(size)

    @staticmethod
    def scene(source, phase=0, center_ring=False, top=None, bottom=520):
        frame = Image.new("RGB", (640, 700), (28, 28, 28))
        width = 160
        height = round(width * source.height / source.width)
        thumbnail = source.resize((width, height))
        if top is not None:
            frame.paste(thumbnail, (450, top))
        if bottom is not None:
            frame.paste(thumbnail, (450, bottom))
            if center_ring:
                cy = bottom + height // 2
                ImageDraw.Draw(frame).ellipse((521, cy-9, 539, cy+9), outline=(210, 210, 210), width=3)
        # This is outside the photo and every status region. It invalidates
        # the whole-frame shortcut just like an unrelated UI animation.
        frame.putpixel((15, 17), (phase % 256, 60, 90))
        return frame

    def test_changing_background_reuses_materials_without_skipping_full_search(self):
        source = self.source()
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        self.addCleanup(verifier.end_image)
        before = self.scene(source)
        status = verifier.evaluate(before, source, before.copy(), [160])
        self.assertTrue(status.is_upload_complete)
        counters = dict(verifier.performance_counters)
        # Every warm frame must still scan globally, but no cached template
        # requires another conversion of the 18 MiB decoded source.
        with patch.object(source, "convert", side_effect=AssertionError("warm full-source copy")):
            for phase in (1, 2, 3):
                frame = self.scene(source, phase)
                status = verifier.evaluate(frame, source, before, [160])
                self.assertTrue(status.is_upload_complete)
                before = frame
        self.assertEqual(verifier.performance_counters["full_searches"] - counters["full_searches"], 3)
        self.assertEqual(verifier.performance_counters["source_array_builds"], counters["source_array_builds"])
        self.assertEqual(verifier.performance_counters["overlay_resizes"], counters["overlay_resizes"])
        self.assertIsNone(verifier._source_scope_arrays)
        self.assertIsNone(verifier._source_arrays)

    def test_new_center_ring_is_detected_after_reference_templates_are_cached(self):
        source = self.source()
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        self.addCleanup(verifier.end_image)
        quiet = self.scene(source)
        verifier.evaluate(quiet, source, quiet.copy(), [160])
        count = verifier.performance_counters["overlay_resizes"]
        frame = self.scene(source, 1, center_ring=True)
        # Use a frozen ring frame as previous: zero motion must not approve it.
        status = verifier.evaluate(frame, source, frame.copy(), [160])
        self.assertTrue(status.has_static_spinner)
        self.assertFalse(status.is_upload_complete)
        self.assertEqual(verifier.performance_counters["overlay_resizes"], count)
        self.assertGreater(verifier.performance_counters["overlay_cache_hits"], 0)

    def test_new_lower_duplicate_is_not_hidden_by_a_warm_global_search(self):
        source = self.source()
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        self.addCleanup(verifier.end_image)
        old = self.scene(source, top=50, bottom=None)
        verifier.evaluate(old, source, old.copy(), [160])
        current = self.scene(source, 1, center_ring=True, top=50)
        status = verifier.evaluate(current, source, current.copy(), [160])
        self.assertGreater(status.bbox[1], 500)
        self.assertTrue(status.has_static_spinner)
        self.assertFalse(status.is_upload_complete)

    def test_cold_multiwidth_search_builds_large_arrays_once_and_releases_them(self):
        source = self.source()
        frame = self.scene(source)
        verifier = VisualBubbleVerifier()
        self.assertTrue(verifier.locate_thumbnail(frame, source, [180, 160, 140])[0])
        self.assertEqual(verifier.performance_counters["source_array_builds"], 1)
        self.assertIsNone(verifier._source_arrays)
        self.assertIsNone(verifier._source_scope_arrays)
        self.assertIsNone(verifier._source_scope)

    def test_native_matching_exception_releases_large_temporary_arrays(self):
        source = self.source()
        verifier = VisualBubbleVerifier()
        with patch("wechat_sender.image_utils.cv2.matchTemplate", side_effect=RuntimeError("synthetic match failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic match failure"):
                verifier.locate_thumbnail(self.scene(source), source, [160])
        self.assertIsNone(verifier._source_arrays)
        self.assertIsNone(verifier._source_scope_arrays)
        self.assertIsNone(verifier._source_scope)

    def test_editing_the_source_circle_invalidates_overlay_references(self):
        source = self.source((160, 120))
        verifier = VisualBubbleVerifier()
        frame = self.scene(source)
        bbox = (450, 520, 160, 120)
        self.assertFalse(verifier.detect_center_upload_overlay(frame, source, bbox))
        original_key = verifier._source_key
        # The circle now belongs to the photograph, and must not be mistaken
        # for new UI by retaining the former source's reference pixels.
        ImageDraw.Draw(source).ellipse((71, 51, 89, 69), outline=(210, 210, 210), width=3)
        self.assertFalse(verifier.detect_center_upload_overlay(self.scene(source), source, bbox))
        self.assertNotEqual(original_key, verifier._source_key)
        self.assertEqual(verifier.performance_counters["overlay_resizes"], 4)

    def test_palette_edits_invalidate_overlay_references(self):
        source = self.source((160, 120)).quantize(colors=8)
        verifier = VisualBubbleVerifier()
        bbox = (450, 520, 160, 120)
        verifier.detect_center_upload_overlay(self.scene(source), source, bbox)
        key = verifier._source_key
        palette = source.getpalette()
        source.putpalette([(value + 70) % 256 for value in palette])
        verifier.detect_center_upload_overlay(self.scene(source), source, bbox)
        self.assertNotEqual(key, verifier._source_key)
        self.assertEqual(verifier.performance_counters["overlay_resizes"], 4)

    def test_overlay_cache_is_bounded_and_does_not_retain_the_original(self):
        source = self.source((160, 120))
        reference = weakref.ref(source)
        verifier = VisualBubbleVerifier()
        verifier.begin_image(source)
        for width in range(200, 900, 30):
            height = width * 3 // 4
            frame = Image.new("RGB", (width+40, height+40), (28, 28, 28))
            frame.paste(source.resize((width, height)), (20, 20))
            self.assertFalse(verifier.detect_center_upload_overlay(frame, source, (20, 20, width, height)))
        self.assertLessEqual(verifier._overlay_bytes, 16*1024*1024)
        self.assertLessEqual(len(verifier._overlay_templates), 16)
        verifier.end_image()
        del source
        gc.collect()
        self.assertIsNone(reference())


if __name__ == "__main__":
    unittest.main()
