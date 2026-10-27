"""Reference sending contract: matching upload state, without history identity gates.

All Windows, clipboard and keyboard boundaries are mocked or dry-run. These
tests intentionally do not construct the Windows adapter or inspect a desktop.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from wechat_sender.image_utils import BubbleVisualStatus, scan_image_folder
from wechat_sender.job_store import ItemStage, SequentialSendEngine
from wechat_sender.wechat_adapter import BaseWeChatAdapter, WindowsWeChatAdapter


BOX = (300, 180, 160, 120)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def status(*, spinner=False, motion=0.0, failure=False):
    # Classification results are mutable while the outer quiet timer waits.
    return BubbleVisualStatus(
        matched=True, similarity=0.96, bbox=BOX,
        has_failure_badge=failure, has_static_spinner=spinner,
        spinner_motion_score=motion, is_upload_complete=True,
        reason="synthetic reference observation",
    )


class TestReferencePipeline(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "photo.png"
        Image.new("RGB", (160, 120), (35, 110, 205)).save(self.path)
        self.chat = Image.new("RGB", (500, 400), (28, 28, 28))
        self.chat.paste(Image.open(self.path), BOX[:2])

    def adapter(self):
        adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
        BaseWeChatAdapter.__init__(adapter)
        adapter.verifier = Mock(motion_quiet_threshold=1.8)
        adapter.verifier.evaluate.side_effect = lambda *args, **kwargs: status()
        adapter.verifier.locate_thumbnail.side_effect = AssertionError("history scanning is not required")
        adapter._pre_send_shot = self.chat.copy()
        adapter._pre_send_wal = None
        adapter._last_locked_bbox = None
        adapter._hwnd = 123
        adapter.quiet_confirm_sec = 1.5
        adapter.post_send_grace_sec = 0.0
        adapter._capture_chat_region = Mock(return_value=self.chat.copy())
        adapter._is_new_outgoing_bubble = Mock(side_effect=AssertionError("pixel novelty is not required"))
        adapter.wal_tracker = Mock()
        adapter.wal_tracker.snapshot.side_effect = AssertionError("cache writes are not upload completion")
        adapter._paste_and_submit = Mock(side_effect=AssertionError("verification must not submit anything"))
        return adapter

    def verify(self, adapter, clock=None):
        clock = clock or Clock()
        with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", clock.sleep
        ), patch("wechat_sender.wechat_adapter.ImageSubmissionTracker", create=True,
                 side_effect=AssertionError("history tracker is not required")) as tracker:
            result = adapter.verify_image_sent(self.path, timeout_sec=0.5, stable_duration_sec=1.5)
        tracker.assert_not_called()
        return result, clock

    def test_quiet_matching_image_does_not_require_history_scan_or_tracker(self):
        adapter = self.adapter()
        result, clock = self.verify(adapter)
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(clock.now, 1.5)
        adapter.verifier.locate_thumbnail.assert_not_called()
        adapter._is_new_outgoing_bubble.assert_not_called()
        adapter.wal_tracker.snapshot.assert_not_called()

    def test_already_submitted_image_does_not_require_a_pre_send_snapshot(self):
        adapter = self.adapter()
        adapter._pre_send_shot = None
        result, _ = self.verify(adapter)
        self.assertTrue(result.is_upload_complete, result.reason)

    def test_weak_similarity_in_history_cannot_veto_a_current_exact_match(self):
        adapter = self.adapter()
        adapter.verifier.locate_thumbnail = Mock(return_value=(True, 0.74, (300, 10, 160, 120)))
        result, _ = self.verify(adapter)
        self.assertTrue(result.is_upload_complete, result.reason)
        adapter.verifier.locate_thumbnail.assert_not_called()

    def test_same_pixels_can_pass_the_final_upload_recheck(self):
        adapter = self.adapter()
        adapter._last_confirmed_path = os.path.normcase(str(self.path.resolve()))
        adapter._last_confirmed_frame = self.chat.copy()
        adapter._last_locked_bbox = BOX
        adapter._submission_tracker = Mock()
        adapter._submission_tracker.observe.side_effect = AssertionError("do not ask history again")
        ready, reason = adapter.confirm_image_before_name(self.path)
        self.assertTrue(ready, reason)
        adapter._submission_tracker.observe.assert_not_called()
        adapter._is_new_outgoing_bubble.assert_not_called()

    def test_spinner_disappearance_still_requires_the_complete_quiet_interval(self):
        adapter = self.adapter()
        clock = Clock()
        adapter.verifier.evaluate.side_effect = lambda *args, **kwargs: status(spinner=clock.now < 2.0)
        result, _ = self.verify(adapter, clock)
        self.assertTrue(result.is_upload_complete, result.reason)
        self.assertGreaterEqual(clock.now, 3.5)

    def test_spinner_motion_or_failure_never_allows_name_submission(self):
        for observation in ({"spinner": True}, {"motion": 3.0}, {"failure": True}):
            with self.subTest(observation=observation):
                adapter = self.adapter()
                adapter.ensure_foreground_and_target = Mock(return_value=(True, "synthetic foreground"))
                adapter.send_image = Mock()
                adapter.verifier.evaluate.side_effect = lambda *args, **kwargs: status(**observation)
                adapter.send_text_and_verify = Mock(side_effect=AssertionError("upload is not complete"))
                items, _ = scan_image_folder(self.folder)
                engine = SequentialSendEngine(adapter, jobs_dir=self.folder / "jobs")
                job = engine.create_job(self.folder, items, target_chat="synthetic chat")
                clock = Clock()
                with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
                    "wechat_sender.wechat_adapter.time.sleep", clock.sleep
                ):
                    completed, _ = engine.run_job(job)
                self.assertFalse(completed)
                adapter.send_text_and_verify.assert_not_called()
                self.assertEqual(job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)

    def test_final_recheck_still_vetoes_spinner_motion_and_failure(self):
        for observation in ({"spinner": True}, {"motion": 3.0}, {"failure": True}):
            with self.subTest(observation=observation):
                adapter = self.adapter()
                adapter._last_confirmed_path = os.path.normcase(str(self.path.resolve()))
                adapter._last_confirmed_frame = self.chat.copy()
                adapter._last_locked_bbox = BOX
                adapter.verifier.evaluate.side_effect = lambda *args, **kwargs: status(**observation)
                self.assertFalse(adapter.confirm_image_before_name(self.path)[0])


if __name__ == "__main__":
    unittest.main()
