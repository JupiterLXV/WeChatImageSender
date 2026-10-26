"""Offline upload-indicator regressions; only generated images are inspected."""

import math
import io
import unittest
from unittest.mock import Mock

import numpy as np
from PIL import Image, ImageDraw

from wechat_sender.image_utils import VisualBubbleVerifier


class TestUploadIndicators(unittest.TestCase):
    def make_scene(self, scale=1.0, light=False, height=None):
        background = (244, 244, 244) if light else (30, 30, 30)
        indicator = (130, 130, 130) if light else (210, 210, 210)
        width, thumb_height = int(160 * scale), height or int(128 * scale)
        x, y = 450, 100
        yy, xx = np.mgrid[0:192, 0:240]
        source = Image.fromarray(np.stack((xx * 255 // 240, yy * 255 // 192,
                                          (xx + yy) * 127 // 432), axis=2).astype(np.uint8))
        scene = Image.new('RGB', (1500, max(900, y + thumb_height + 50)), background)
        scene.paste(source.resize((width, thumb_height)), (x, y))
        return source, scene, (x, y, width, thumb_height), indicator

    def ring(self, scene, bbox, color, scale=1.0, side='left', vertical=.5, phase=None):
        x, y, width, height = bbox
        cx = x - int(25 * scale) if side == 'left' else x + width + int(25 * scale)
        cy = y + int(height * vertical)
        radius = max(4, int(round(9 * scale)))
        pen = max(1, int(round(2 * scale)))
        draw = ImageDraw.Draw(scene)
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        if phase is None:
            draw.ellipse(box, outline=color, width=pen)
        else:
            draw.arc(box, phase, phase + 110, fill=color, width=pen)

    def evaluate_at(self, source, current, previous, bbox):
        verifier = VisualBubbleVerifier()
        verifier.locate_thumbnail = Mock(return_value=(True, .98, bbox))
        return verifier.evaluate(current, source, previous)

    def test_frozen_spinner_blocks_at_common_scales_and_both_themes(self):
        for scale in (1.0, 1.25, 1.5, 2.0, 3.0):
            for light in (False, True):
                for vertical in (.3, .5, .7):
                    with self.subTest(scale=scale, light=light, vertical=vertical):
                        source, scene, bbox, color = self.make_scene(scale, light)
                        self.ring(scene, bbox, color, scale, vertical=vertical)
                        status = self.evaluate_at(source, scene, scene.copy(), bbox)
                        self.assertTrue(status.has_static_spinner)
                        self.assertFalse(status.is_upload_complete)

    def test_low_contrast_spinner_is_not_treated_as_empty_background(self):
        source, scene, bbox, _color = self.make_scene()
        self.ring(scene, bbox, (55, 55, 55))
        self.assertFalse(self.evaluate_at(source, scene, scene, bbox).is_upload_complete)

    def test_separate_spinner_spokes_are_detected(self):
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                source, scene, bbox, color = self.make_scene(scale)
                cx, cy = bbox[0] - int(25 * scale), bbox[1] + bbox[3] // 2
                draw = ImageDraw.Draw(scene)
                for step in range(12):
                    angle = step * math.pi / 6
                    draw.line((cx + math.cos(angle) * 5 * scale, cy + math.sin(angle) * 5 * scale,
                               cx + math.cos(angle) * 9 * scale, cy + math.sin(angle) * 9 * scale),
                              fill=color, width=max(1, round(2 * scale)))
                self.assertTrue(self.evaluate_at(source, scene, scene, bbox).has_static_spinner)

    def test_right_side_indicator_is_detected_without_top_avatar_false_positive(self):
        for scale in (1.25, 1.5, 2.0):
            with self.subTest(scale=scale):
                source, scene, bbox, color = self.make_scene(scale)
                self.ring(scene, bbox, color, scale, side='right')
                self.assertTrue(self.evaluate_at(source, scene, scene, bbox).has_static_spinner)
        source, scene, bbox, _color = self.make_scene()
        x, y, width, _height = bbox
        ImageDraw.Draw(scene).ellipse((x + width + 10, y + 2, x + width + 48, y + 40), fill=(140, 140, 140))
        status = self.evaluate_at(source, scene, scene.copy(), bbox)
        self.assertFalse(status.has_static_spinner)
        self.assertTrue(status.is_upload_complete)

    def test_small_motion_is_not_diluted_by_large_thumbnail(self):
        source, previous, bbox, color = self.make_scene(height=400)
        # A large photo with a very small animated status icon used to have a
        # whole-region average below the quiet threshold.
        current = previous.copy()
        self.ring(previous, bbox, color, phase=0)
        self.ring(current, bbox, color, phase=160)
        average = float(np.mean(np.abs(np.asarray(previous, dtype=np.float32) -
                                       np.asarray(current, dtype=np.float32))))
        verifier = VisualBubbleVerifier()
        self.assertLess(average, verifier.motion_quiet_threshold)
        self.assertGreater(verifier.compute_spinner_motion(previous, current, bbox),
                           verifier.motion_quiet_threshold)

    def test_center_overlay_animation_blocks_even_without_side_indicator(self):
        source, previous, bbox, color = self.make_scene()
        current = previous.copy()
        x, y, width, height = bbox
        cx, cy = x + width // 2, y + height // 2
        ImageDraw.Draw(previous).arc((cx - 9, cy - 9, cx + 9, cy + 9), 0, 110, fill=color, width=3)
        ImageDraw.Draw(current).arc((cx - 9, cy - 9, cx + 9, cy + 9), 160, 270, fill=color, width=3)
        status = self.evaluate_at(source, current, previous, bbox)
        self.assertFalse(status.is_upload_complete)
        self.assertGreater(status.spinner_motion_score, VisualBubbleVerifier().motion_quiet_threshold)

    def test_photo_circle_is_not_classified_as_upload_indicator(self):
        source, scene, bbox, color = self.make_scene()
        ImageDraw.Draw(source).ellipse((60, 45, 180, 147), outline=color, width=6)
        x, y, width, height = bbox
        scene.paste(source.resize((width, height)), (x, y))
        status = self.evaluate_at(source, scene, scene.copy(), bbox)
        self.assertFalse(status.has_static_spinner)
        self.assertTrue(status.is_upload_complete)

    def test_disappearing_spinner_requires_a_further_quiet_frame(self):
        source, empty, bbox, color = self.make_scene()
        uploading = empty.copy()
        self.ring(uploading, bbox, color)
        self.assertFalse(self.evaluate_at(source, empty, uploading, bbox).is_upload_complete)
        self.assertTrue(self.evaluate_at(source, empty, empty.copy(), bbox).is_upload_complete)

    def test_small_failure_badge_on_tall_photo_is_not_diluted(self):
        source, scene, bbox, _color = self.make_scene(height=600)
        x, y, _width, height = bbox
        ImageDraw.Draw(scene).ellipse((x - 35, y + height // 2 - 9,
                                       x - 17, y + height // 2 + 9), fill=(220, 35, 35))
        status = self.evaluate_at(source, scene, scene, bbox)
        self.assertTrue(status.has_failure_badge)
        self.assertFalse(status.is_upload_complete)

    def test_changed_capture_size_is_not_quiet(self):
        _source, scene, bbox, _color = self.make_scene()
        verifier = VisualBubbleVerifier()
        self.assertGreater(verifier.compute_spinner_motion(scene.resize((750, 450)), scene, bbox),
                           verifier.motion_quiet_threshold)

    def test_large_high_dpi_thumbnails_match_without_a_preknown_width(self):
        yy, xx = np.mgrid[0:400, 0:600]
        pixels = np.stack((xx * 255 // 600, yy * 255 // 400,
                           ((xx // 36 + yy // 32) % 2) * 150 + 40), axis=2).astype(np.uint8)
        source = Image.fromarray(pixels)
        for width in (450, 600):
            with self.subTest(width=width):
                scene = Image.new('RGB', (1100, 800), (30, 30, 30))
                height = round(width * 2 / 3)
                scene.paste(source.resize((width, height), Image.Resampling.LANCZOS), (1050 - width, 250))
                verifier = VisualBubbleVerifier()
                status = verifier.evaluate(scene, source, scene.copy())
                self.assertTrue(status.matched)
                self.assertTrue(status.is_upload_complete)
                self.assertGreater(status.similarity, .85)
                self.assertLessEqual(abs(status.bbox[2] - width), 10)

    def test_explicit_width_keeps_the_locked_matching_path(self):
        source, scene, bbox, _color = self.make_scene()
        verifier = VisualBubbleVerifier()
        original = verifier._match_single_source_template
        verifier._match_single_source_template = Mock(wraps=original)
        matched, _similarity, _box = verifier.locate_thumbnail(scene, source, candidate_widths=[bbox[2]])
        self.assertTrue(matched)
        self.assertEqual(verifier._match_single_source_template.call_args.args[-1], [bbox[2]])

    def test_frozen_white_and_gray_center_rings_veto_completion(self):
        for color in ((255, 255, 255), (180, 180, 180)):
            for scale in (1.0, 1.5, 2.0):
                with self.subTest(color=color, scale=scale):
                    source, scene, bbox, _indicator = self.make_scene(scale)
                    x, y, width, height = bbox
                    cx, cy = x + width // 2, y + height // 2
                    radius = round(9 * scale)
                    ImageDraw.Draw(scene).ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                                                  outline=color, width=round(2 * scale))
                    status = self.evaluate_at(source, scene, scene.copy(), bbox)
                    self.assertTrue(status.has_static_spinner)
                    self.assertFalse(status.is_upload_complete)

    def test_original_photo_rings_and_letters_survive_jpeg_and_resize(self):
        for kind in ('ring', 'letters'):
            for background, foreground in ((244, 35), (30, 220)):
                for quality in (95, 70, 35, 15):
                    for method in (Image.Resampling.BILINEAR, Image.Resampling.LANCZOS):
                        with self.subTest(kind=kind, background=background, quality=quality, method=method):
                            source, scene, bbox, _color = self.make_scene()
                            source = Image.new('RGB', source.size, (background,) * 3)
                            draw = ImageDraw.Draw(source)
                            if kind == 'ring':
                                draw.ellipse((103, 79, 137, 113), outline=(foreground,) * 3, width=4)
                            else:
                                draw.text((99, 89), 'OO 0123', fill=(foreground,) * 3)
                            x, y, width, height = bbox
                            rendered = source.resize((width, height), method)
                            encoded = io.BytesIO()
                            rendered.save(encoded, format='JPEG', quality=quality)
                            encoded.seek(0)
                            scene.paste(Image.open(encoded).convert('RGB'), (x, y))
                            status = self.evaluate_at(source, scene, scene.copy(), bbox)
                            self.assertFalse(status.has_static_spinner)
                            self.assertTrue(status.is_upload_complete)

    def test_center_ring_requires_matching_photo_surroundings(self):
        source, scene, bbox, _color = self.make_scene()
        x, y, width, height = bbox
        ImageDraw.Draw(scene).rectangle((x, y, x + width - 1, y + height - 1), fill=(50, 50, 50))
        cx, cy = x + width // 2, y + height // 2
        ImageDraw.Draw(scene).ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline='white', width=2)
        self.assertFalse(VisualBubbleVerifier().detect_center_upload_overlay(scene, source, bbox))


if __name__ == '__main__':
    unittest.main()
