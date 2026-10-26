"""Synthetic duplicate-image regressions; no desktop or real WeChat access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from wechat_sender.image_utils import VisualBubbleVerifier
from wechat_sender.wechat_adapter import BaseWeChatAdapter, WindowsWeChatAdapter


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestDuplicateUpload(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "repeated.png"
        self.source = Image.new("RGB", (160, 120), (35, 110, 205))
        draw = ImageDraw.Draw(self.source)
        draw.rectangle((15, 15, 75, 70), fill=(220, 80, 40))
        draw.ellipse((90, 45, 140, 100), fill=(235, 220, 105))
        draw.line((10, 110, 145, 10), fill=(235, 245, 250), width=4)
        self.source.save(self.path)

    def scene(self, scale=1.0, *, old_y=None, new_y=None, spinner=False, history=None):
        frame = Image.new("RGB", (round(500 * scale), round(650 * scale)), (28, 28, 28))
        thumb = self.source.resize((round(self.source.width * scale), round(self.source.height * scale)))
        if old_y is not None:
            old = history or self.source
            frame.paste(old.resize(thumb.size), (round(300 * scale), round(old_y * scale)))
        if new_y is not None:
            frame.paste(thumb, (round(300 * scale), round(new_y * scale)))
        if spinner:
            draw = ImageDraw.Draw(frame)
            draw.ellipse(tuple(round(v * scale) for v in (267, new_y + 45, 283, new_y + 61)),
                         outline=(185, 185, 185), width=max(2, round(3 * scale)))
        return frame

    def adapter(self, before, frames, clock, stop_at=7.0):
        # Bypass all real Windows construction and replace every system boundary.
        adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
        BaseWeChatAdapter.__init__(adapter)
        adapter.verifier = VisualBubbleVerifier()
        adapter._hwnd = 123
        adapter._pre_send_shot = before
        adapter._last_locked_bbox = None
        adapter._pre_send_wal = None
        adapter.quiet_confirm_sec = 1.5
        adapter.post_send_grace_sec = 0.0
        adapter._capture_chat_region = Mock(side_effect=lambda: frames(clock.now))
        adapter._paste_and_submit = Mock(side_effect=AssertionError("verification must not type"))
        adapter.set_stop_callback(lambda: clock.now >= stop_at)
        return adapter

    def verify(self, before, frames, *, stop_at=7.0):
        clock = Clock()
        adapter = self.adapter(before, frames, clock, stop_at)
        with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", clock.sleep
        ):
            result = adapter.verify_image_sent(self.path, timeout_sec=0.5, stable_duration_sec=1.5)
        return result, adapter, clock

    def test_repeated_new_image_waits_for_its_own_spinner_then_completes(self):
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                before = self.scene(scale, old_y=250)
                loading = self.scene(scale, old_y=50, new_y=470, spinner=True)
                finished = self.scene(scale, old_y=50, new_y=470)
                result, adapter, clock = self.verify(before, lambda t: loading if t < 2.0 else finished)
                self.assertTrue(result.is_upload_complete, result.reason)
                self.assertGreaterEqual(clock.now, 3.5)
                self.assertGreaterEqual(result.bbox[1], round(450 * scale))
                self.assertIsNotNone(adapter._last_confirmed_path)

    def test_static_spinner_on_new_duplicate_never_completes_from_old_exact_copy(self):
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                before = self.scene(scale, old_y=250)
                loading = self.scene(scale, old_y=50, new_y=470, spinner=True)
                result, adapter, _ = self.verify(before, lambda t: loading, stop_at=3.5)
                self.assertFalse(result.is_upload_complete)
                self.assertIsNone(adapter._last_confirmed_path)

    def test_scroll_of_history_without_any_added_message_is_not_a_new_upload(self):
        before = self.scene(old_y=250)
        moved = self.scene(old_y=50)
        result, adapter, _ = self.verify(before, lambda t: moved, stop_at=3.5)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_repeated_image_appended_without_scroll_can_complete(self):
        before = self.scene(old_y=50)
        appended = self.scene(old_y=50, new_y=470)
        result, _, _ = self.verify(before, lambda t: appended)
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(result.bbox[1], 450)

    def test_repeated_portrait_thumbnail_can_complete(self):
        self.source = self.source.transpose(Image.Transpose.ROTATE_90)
        self.source.save(self.path)
        before = self.scene(old_y=50)
        appended = self.scene(old_y=50, new_y=470)
        result, _, _ = self.verify(before, lambda t: appended)
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(result.bbox[1], 450)

    def test_new_spinner_then_disappearance_at_same_viewport_position_can_complete(self):
        # A full chat can scroll by exactly one image height: the final new
        # photo then occupies the old photo's coordinates. Only the observed
        # upload state identifies this submission; final-frame diff is zero.
        before = self.scene(old_y=470)
        loading = self.scene(new_y=470, spinner=True)
        result, adapter, clock = self.verify(before, lambda t: loading if t < 2.0 else before.copy())
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(clock.now, 3.5)
        self.assertIsNotNone(adapter._last_confirmed_path)
        ready, reason = adapter.confirm_image_before_name(self.path)
        self.assertTrue(ready, reason)

    def center_loading_scene(self, scale):
        # Keep a flat center so the overlay differs from its source rather
        # than accidentally reusing a photographed circle or source edge.
        ImageDraw.Draw(self.source).rectangle((50, 30, 110, 90), fill=(35, 110, 205))
        self.source.save(self.path)
        before = self.scene(scale, old_y=250)
        loading = self.scene(scale, old_y=50, new_y=470)
        ImageDraw.Draw(loading).ellipse(tuple(round(v * scale) for v in (368, 518, 392, 542)),
                                       outline=(245, 245, 245), width=max(2, round(3 * scale)))
        finished = self.scene(scale, old_y=50, new_y=470)
        return before, loading, finished

    def test_center_upload_ring_on_new_duplicate_blocks_the_old_completed_copy(self):
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                before, loading, _ = self.center_loading_scene(scale)
                result, adapter, _ = self.verify(before, lambda t: loading, stop_at=3.5)
                self.assertFalse(result.is_upload_complete)
                self.assertIsNone(adapter._last_confirmed_path)
                self.assertGreaterEqual(adapter.last_verify_bbox[1], round(450 * scale))
                self.assertTrue(adapter.last_verify_status.has_static_spinner)

    def test_center_upload_ring_disappearance_allows_the_new_duplicate_to_complete(self):
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                before, loading, finished = self.center_loading_scene(scale)
                result, adapter, clock = self.verify(before, lambda t: loading if t < 2.0 else finished)
                self.assertTrue(result.is_upload_complete, result.reason)
                self.assertGreaterEqual(clock.now, 3.5)
                self.assertGreaterEqual(result.bbox[1], round(450 * scale))
                ready, reason = adapter.confirm_image_before_name(self.path)
                self.assertTrue(ready, reason)

    def test_historical_similar_image_does_not_block_the_exact_new_image(self):
        # Keep composition but alter a large section of the old photo. Its
        # weak similarity must not veto the exact new copy in the message below.
        history = self.source.copy()
        ImageDraw.Draw(history).rectangle((0, 0, 60, 119), fill=(165, 45, 150))
        before = self.scene(old_y=50, history=history)
        appended = self.scene(old_y=50, new_y=470, history=history)
        matched, similarity, _ = VisualBubbleVerifier().locate_thumbnail(before, self.source)
        self.assertTrue(matched)
        self.assertLess(similarity, 0.85)
        result, _, _ = self.verify(before, lambda t: appended)
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(result.bbox[1], 450)

    def test_unchanged_historical_duplicate_is_never_new_evidence(self):
        before = self.scene(old_y=470)
        result, adapter, _ = self.verify(before, lambda t: before.copy(), stop_at=3.5)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_spinner_already_in_history_before_submission_is_not_new_evidence(self):
        before = self.scene(new_y=470, spinner=True)
        old_finished = self.scene(old_y=470)
        result, adapter, _ = self.verify(before, lambda t: before if t < 2.0 else old_finished,
                                         stop_at=4.5)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)


if __name__ == "__main__":
    unittest.main()
