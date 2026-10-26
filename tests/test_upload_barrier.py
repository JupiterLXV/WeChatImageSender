"""Offline sequencing regressions: synthetic frames and mocked desktop APIs only."""

import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from wechat_sender.image_utils import BubbleVisualStatus, VisualBubbleVerifier, scan_image_folder
from wechat_sender.capture_win32 import ChatFrame
from wechat_sender.job_store import ItemStage, ManualDecision, SequentialSendEngine
from wechat_sender.wechat_adapter import BaseWeChatAdapter, DemoWeChatAdapter, WindowsWeChatAdapter


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


BOX = (300, 180, 160, 120)


def frame_status(*, spinner=False, motion=0.0, complete=True, matched=True, failure=False):
    # Every observation is a new mutable result, as in the real classifier.
    return BubbleVisualStatus(
        matched=matched, similarity=0.95 if matched else 0.30,
        bbox=BOX if matched else None,
        has_failure_badge=failure, has_static_spinner=spinner,
        spinner_motion_score=motion, is_upload_complete=complete,
        reason="synthetic spinner" if spinner else "synthetic quiet frame",
    )


def mocked_windows_adapter(blank, chat):
    # Do not call the real Windows constructor: no discovery, activation,
    # clipboard access, screenshots or keyboard injection are permitted here.
    adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
    BaseWeChatAdapter.__init__(adapter)
    adapter.verifier = Mock(motion_quiet_threshold=1.8)
    adapter.verifier.evaluate.side_effect = lambda *a, **kw: frame_status()
    adapter.verifier.locate_thumbnail.return_value = (False, 0.0, None)
    adapter.wal_tracker = Mock()
    adapter.wal_tracker.snapshot.side_effect = AssertionError("WAL is not upload evidence")
    adapter.quiet_confirm_sec = 0.01
    adapter.post_send_grace_sec = 0.0
    adapter._pre_send_shot = blank
    adapter._pre_send_wal = None
    adapter._last_locked_bbox = None
    adapter._hwnd = 123
    adapter._capture_chat_region = Mock(return_value=chat)
    adapter._paste_and_submit = Mock()
    return adapter


class TestUploadCompletionBarrier(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        image = Image.new("RGB", (160, 120), (35, 110, 205))
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 15, 75, 70), fill=(220, 80, 40))
        draw.ellipse((90, 45, 140, 100), fill=(235, 220, 105))
        self.path = self.folder / "01.png"
        image.save(self.path)
        self.blank = Image.new("RGB", (500, 400), (28, 28, 28))
        self.chat = self.blank.copy()
        self.chat.paste(image, BOX[:2])

    def verify(self, adapter, clock=None, stable=0.01):
        clock = clock or VirtualClock()
        with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", clock.sleep
        ):
            result = adapter.verify_image_sent(self.path, timeout_sec=0.5, stable_duration_sec=stable)
        return result, clock

    def test_frozen_spinner_is_not_completion_even_when_motion_is_zero(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.verifier.evaluate.side_effect = lambda *a, **kw: frame_status(spinner=True, complete=False)
        result, _ = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        self.assertTrue(result.has_static_spinner)
        self.assertIsNone(adapter._last_confirmed_path)
        adapter.wal_tracker.snapshot.assert_not_called()

    def test_motion_in_loading_indicator_blocks_quiet_count(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.verifier.evaluate.side_effect = lambda *a, **kw: frame_status(motion=2.5, complete=False)
        result, _ = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_spinner_reappearance_restarts_the_entire_quiet_interval(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        clock = VirtualClock()
        observations = []

        def evaluate(*args, **kwargs):
            spinner = 1.45 <= clock.now < 1.85
            observations.append((clock.now, spinner))
            return frame_status(spinner=spinner, complete=not spinner)

        adapter.verifier.evaluate.side_effect = evaluate
        result, _ = self.verify(adapter, clock)
        self.assertTrue(result.is_upload_complete)
        last_spinner = max(t for t, spinner in observations if spinner)
        first_quiet_after_spinner = min(t for t, spinner in observations if not spinner and t > last_spinner)
        self.assertGreaterEqual(clock.now - first_quiet_after_spinner, 1.5 - 1e-9)
        self.assertGreaterEqual(clock.now, 3.3)

    def test_disk_activity_does_not_shorten_minimum_quiet_interval(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.wal_tracker.snapshot.side_effect = range(1000)
        result, clock = self.verify(adapter)
        self.assertTrue(result.is_upload_complete)
        self.assertGreaterEqual(clock.now, 0.75 + 1.5)
        adapter.wal_tracker.snapshot.assert_not_called()

    def test_larger_requested_quiet_interval_is_honored(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        result, clock = self.verify(adapter, stable=2.5)
        self.assertTrue(result.is_upload_complete)
        self.assertGreaterEqual(clock.now, 0.75 + 2.5)

    def test_historical_matching_image_does_not_confirm_a_new_submission(self):
        adapter = mocked_windows_adapter(self.chat.copy(), self.chat)
        result, _ = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_scrolled_historical_duplicate_is_not_the_loading_new_image(self):
        source = Image.open(self.path).convert("RGB")
        before = Image.new("RGB", (500, 650), (28, 28, 28))
        before.paste(source, (300, 250))
        after = Image.new("RGB", (500, 650), (28, 28, 28))
        # Submission scrolls the old completed copy upward. A newly submitted
        # copy below still has its upload ring, while the old copy looks exact.
        after.paste(source, (300, 50))
        after.paste(source, (300, 470))
        ImageDraw.Draw(after).ellipse((267, 515, 283, 531), outline=(185, 185, 185), width=3)
        adapter = mocked_windows_adapter(before, after)
        adapter.verifier = VisualBubbleVerifier()
        result, _ = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_weak_template_match_is_not_promoted_by_frame_changes(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.verifier.evaluate.side_effect = lambda *a, **kw: frame_status(matched=False, complete=True)
        result, _ = self.verify(adapter)
        self.assertFalse(result.matched)
        self.assertFalse(result.is_upload_complete)

    def test_f8_cancellation_exits_without_capturing_more_frames(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.set_stop_callback(lambda: True)
        result, clock = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        self.assertEqual(clock.now, 0.0)
        adapter._capture_chat_region.assert_not_called()

    def test_failed_pre_submit_capture_does_not_paste_any_image(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.clipboard = Mock()
        adapter._capture_chat_region.side_effect = RuntimeError("synthetic unavailable capture")
        with self.assertRaisesRegex(RuntimeError, "unavailable capture"):
            adapter.send_image(self.path, "CF_HDROP", "ENTER")
        adapter._paste_and_submit.assert_not_called()
        adapter.clipboard.set_image_file_hdrop.assert_not_called()

    def test_last_moment_spinner_blocks_name_and_invalidates_cached_confirmation(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._last_confirmed_path = os.path.normcase(str(self.path.resolve()))
        adapter._last_confirmed_frame = self.chat.copy()
        adapter._last_locked_bbox = BOX
        adapter.verifier.evaluate.side_effect = lambda *a, **kw: frame_status(spinner=True, complete=True)
        ready, _ = adapter.confirm_image_before_name(self.path)
        self.assertFalse(ready)
        self.assertIsNone(adapter._last_confirmed_path)

    def test_last_probe_requires_same_image_and_visible_bubble(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._last_confirmed_path = os.path.normcase(str(self.path.resolve()))
        adapter._last_confirmed_frame = self.chat.copy()
        adapter._last_locked_bbox = BOX
        self.assertTrue(adapter.confirm_image_before_name(self.path)[0])
        adapter._last_locked_bbox = (300, 20, 160, 120)
        self.assertFalse(adapter.confirm_image_before_name(self.path)[0])

    def test_uia_exception_after_paste_does_not_replay_with_win32_backend(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._wait_or_stop = Mock(return_value=False)
        adapter._send_keys_win32_fallback = Mock()
        adapter._assert_submission_foreground = Mock()
        send_keys = Mock(side_effect=[None, RuntimeError("synthetic Enter failure")])
        fake_auto = SimpleNamespace(SendKeys=send_keys)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
            "sys.modules", {"uiautomation": fake_auto}
        ), patch("wechat_sender.wechat_adapter._uia_thread_scope", return_value=nullcontext()):
            with self.assertRaisesRegex(RuntimeError, "Enter failure"):
                WindowsWeChatAdapter._paste_and_submit(adapter, "ENTER", is_text=False)
        self.assertEqual(send_keys.call_count, 2)
        adapter._send_keys_win32_fallback.assert_not_called()

    def test_capture_signature_change_rejects_the_frame(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.prefer_print_window = False
        adapter._capture_signature = (123, "original position and DPI")
        adapter._capture_backend = "screen"
        changed = ChatFrame(self.chat, (123, "new position or DPI"), "screen")
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch(
            "wechat_sender.wechat_adapter.capture_chat_frame", return_value=changed
        ):
            with self.assertRaisesRegex(RuntimeError, "位置、缩放"):
                WindowsWeChatAdapter._capture_chat_region(adapter)
        self.assertEqual(adapter._capture_signature, (123, "original position and DPI"))

    def test_capture_backend_change_rejects_the_frame(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter.prefer_print_window = True
        adapter._capture_signature = (123, "same geometry")
        adapter._capture_backend = "printwindow"
        changed = ChatFrame(self.chat, adapter._capture_signature, "screen")
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch(
            "wechat_sender.wechat_adapter.capture_chat_frame", return_value=changed
        ):
            with self.assertRaisesRegex(RuntimeError, "截图方式"):
                WindowsWeChatAdapter._capture_chat_region(adapter)
        self.assertEqual(adapter._capture_backend, "printwindow")

    def test_missing_window_never_requests_any_screenshot(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._hwnd = None
        adapter._image_verify_hwnd = None
        adapter._find_wechat_hwnd = Mock(return_value=None)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch(
            "wechat_sender.wechat_adapter.capture_chat_frame"
        ) as capture:
            with self.assertRaisesRegex(RuntimeError, "未定位"):
                WindowsWeChatAdapter._capture_chat_region(adapter)
        capture.assert_not_called()

    def test_unknown_composer_boundary_rejects_automatic_completion(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        uncertain = self.chat.copy()
        uncertain.info["capture_layout"] = "fallback"
        adapter._capture_chat_region.return_value = uncertain
        result, _ = self.verify(adapter)
        self.assertFalse(result.is_upload_complete)
        adapter.verifier.evaluate.assert_not_called()
        self.assertIsNone(adapter._last_confirmed_path)

    def test_unknown_composer_boundary_rejects_last_probe(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._last_confirmed_path = os.path.normcase(str(self.path.resolve()))
        adapter._last_confirmed_frame = self.chat.copy()
        adapter._last_locked_bbox = BOX
        uncertain = self.chat.copy()
        uncertain.info["capture_layout"] = "fallback"
        adapter._capture_chat_region.return_value = uncertain
        self.assertFalse(adapter.confirm_image_before_name(self.path)[0])
        adapter.verifier.evaluate.assert_not_called()

    def test_gui_window_patch_keeps_the_adapter_name_submission_method(self):
        # Installing the patch only binds methods; no window discovery is run.
        from wechat_sender.gui import _install_robust_window_detector
        adapter = mocked_windows_adapter(self.blank, self.chat)
        original = adapter.send_text_and_verify.__func__
        _install_robust_window_detector(adapter)
        self.assertIs(adapter.send_text_and_verify.__func__, original)

    def test_win32_fallback_stop_after_paste_never_presses_enter(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._wait_or_stop = Mock(return_value=True)
        keyboard = Mock()
        keyboard.GetForegroundWindow.return_value = 123
        with patch("wechat_sender.wechat_adapter.ctypes.windll.user32", keyboard):
            with self.assertRaisesRegex(RuntimeError, "暂停"):
                WindowsWeChatAdapter._send_keys_win32_fallback(adapter, "ENTER", 0.30, 0.08)
        # Ctrl+V can already be in the composer, but Enter must never follow.
        self.assertTrue(keyboard.keybd_event.called)
        self.assertFalse(any(call.args[0] == 0x0D for call in keyboard.keybd_event.call_args_list))

    def test_foreground_changes_after_paste_never_press_enter(self):
        adapter = mocked_windows_adapter(self.blank, self.chat)
        adapter._wait_or_stop = Mock(return_value=False)
        keyboard = Mock()
        keyboard.GetForegroundWindow.side_effect = [123, 999]
        send_keys = Mock()
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch(
            "wechat_sender.wechat_adapter.ctypes.windll.user32", keyboard
        ), patch.dict("sys.modules", {"uiautomation": SimpleNamespace(SendKeys=send_keys)}), patch(
            "wechat_sender.wechat_adapter._uia_thread_scope", return_value=nullcontext()
        ):
            with self.assertRaises(RuntimeError):
                WindowsWeChatAdapter._paste_and_submit(adapter, "ENTER", is_text=False)
        self.assertEqual(send_keys.call_count, 1)
        self.assertEqual(send_keys.call_args.args[0], "{Ctrl}v")


class TestNameSequencingBarrier(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        Image.new("RGB", (60, 40), (32, 110, 200)).save(self.folder / "01.png")
        self.items, _ = scan_image_folder(self.folder)

    def job(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        adapter.verify_image_sent = Mock(side_effect=lambda *a, **kw: frame_status())
        engine = SequentialSendEngine(adapter, jobs_dir=self.folder / "jobs")
        job = engine.create_job(self.folder, self.items, target_chat="文件传输助手")
        return adapter, engine, job

    def assert_no_name_or_next_image(self, adapter, job):
        self.assertEqual(adapter.sent_records, [("IMAGE", "01.png")])
        self.assertEqual(job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)

    def test_final_probe_failure_never_submits_name(self):
        adapter, engine, job = self.job()
        adapter.confirm_image_before_name = Mock(return_value=(False, "synthetic spinner reappeared"))
        adapter.send_text_and_verify = Mock(side_effect=AssertionError("name must not be sent"))
        okay, message = engine.run_job(job)
        self.assertFalse(okay)
        self.assertIn("spinner", message)
        self.assert_no_name_or_next_image(adapter, job)
        adapter.send_text_and_verify.assert_not_called()
        restored = engine.load_job(engine.jobs_dir / f"{job.job_id}.json")
        self.assertEqual(restored.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)

    def test_contradictory_complete_and_spinner_flags_are_rejected_by_engine(self):
        adapter, engine, job = self.job()
        adapter.verify_image_sent.side_effect = lambda *a, **kw: frame_status(spinner=True, complete=True)
        adapter.confirm_image_before_name = Mock(side_effect=AssertionError("not image-confirmed"))
        self.assertFalse(engine.run_job(job)[0])
        self.assert_no_name_or_next_image(adapter, job)
        adapter.confirm_image_before_name.assert_not_called()

    def test_nonfinite_motion_is_never_completion_evidence(self):
        adapter = BaseWeChatAdapter()
        for motion in (float("nan"), float("inf")):
            with self.subTest(motion=motion):
                self.assertFalse(adapter.image_status_allows_name(frame_status(motion=motion)))

    def test_old_confirmed_record_requires_review_then_respects_manual_decision(self):
        adapter, engine, job = self.job()
        job.items[0].stage = ItemStage.IMAGE_CONFIRMED.value
        self.assertEqual(job.items[0].image_confirmation, "")
        self.assertFalse(engine.run_job(job)[0])
        self.assertEqual(adapter.sent_records, [])
        self.assertEqual(job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)
        engine.apply_manual_decision(job, 0, ManualDecision.SEND_NAME_ONLY)
        self.assertEqual(job.items[0].image_confirmation, "manual")
        adapter.confirm_image_before_name = Mock(side_effect=AssertionError("user already checked"))
        self.assertTrue(engine.run_job(job)[0])
        self.assertEqual(adapter.sent_records, [("TEXT", "01")])
        adapter.confirm_image_before_name.assert_not_called()

    def test_stop_between_final_probe_and_name_does_not_submit_name(self):
        adapter, engine, job = self.job()

        def stop_after_probe(_path):
            engine.stop_requested = True
            return True, "OK"

        adapter.confirm_image_before_name = Mock(side_effect=stop_after_probe)
        self.assertFalse(engine.run_job(job)[0])
        self.assertEqual(adapter.sent_records, [("IMAGE", "01.png")])
        self.assertEqual(job.items[0].stage, ItemStage.IMAGE_CONFIRMED.value)


if __name__ == "__main__":
    unittest.main()
