"""Only an explicit pre-paste failure is eligible for an automatic retry."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from wechat_sender.image_utils import scan_image_folder
from wechat_sender.job_store import ItemStage, SequentialSendEngine
from wechat_sender.wechat_adapter import DemoWeChatAdapter, MessagePreparationError


class TestPreparationRetry(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        Image.new("RGB", (60, 40), (40, 110, 200)).save(self.folder / "private-image.png")
        self.items, _ = scan_image_folder(self.folder)
        self.adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        self.adapter.activate_chat("private chat")
        self.engine = SequentialSendEngine(self.adapter, jobs_dir=self.folder / "jobs")
        self.job = self.engine.create_job(self.folder, self.items, "private chat")

    def test_transient_preparation_failure_submits_one_image_and_one_name(self):
        original = self.adapter.send_image
        calls = []
        def transient(*args):
            calls.append(1)
            if len(calls) == 1:
                raise MessagePreparationError("synthetic busy clipboard", retryable=True)
            return original(*args)
        self.adapter.send_image = transient
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assertEqual([kind for kind, _ in self.adapter.sent_records], ["IMAGE", "TEXT"])
        data = json.loads(self.engine.last_performance_log.read_text(encoding="utf-8"))
        self.assertEqual(data["counters"]["prepare_retries"], 1)
        self.assertNotIn("private", json.dumps(data))
        self.assertIsNone(self.adapter.performance_trace)

    def test_repeated_preparation_failure_preserves_pending_without_submission(self):
        self.adapter.send_image = Mock(side_effect=MessagePreparationError("synthetic busy", retryable=True))
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.adapter.send_image.call_count, 2)
        self.assertEqual(self.adapter.sent_records, [])
        self.assertEqual(self.job.items[0].stage, ItemStage.PENDING.value)

    def test_nontransient_preparation_failure_is_not_retried(self):
        self.adapter.send_image = Mock(side_effect=MessagePreparationError("synthetic missing window"))
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.adapter.send_image.call_count, 1)
        self.assertEqual(self.job.items[0].stage, ItemStage.PENDING.value)

    def test_unknown_keyboard_failure_is_never_replayed(self):
        self.adapter.send_image = Mock(side_effect=RuntimeError("synthetic paste may have happened"))
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.adapter.send_image.call_count, 1)
        self.assertEqual(self.job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)

    def test_name_preparation_failure_resumes_without_resending_the_image(self):
        original = self.adapter.send_text_and_verify
        self.adapter.send_text_and_verify = Mock(side_effect=MessagePreparationError("synthetic busy"))
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.job.items[0].stage, ItemStage.IMAGE_CONFIRMED.value)
        self.adapter.send_text_and_verify = original
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assertEqual([kind for kind, _ in self.adapter.sent_records], ["IMAGE", "TEXT"])

    def test_stop_during_retry_never_submits_a_message(self):
        self.adapter.send_image = Mock(side_effect=MessagePreparationError("synthetic busy", retryable=True))
        def stop(_seconds):
            self.engine.stop_requested = True
            return True
        self.adapter._wait_or_stop = stop
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.adapter.send_image.call_count, 1)
        self.assertEqual(self.adapter.sent_records, [])
        self.assertEqual(self.job.items[0].stage, ItemStage.PENDING.value)

    def test_unwritable_metrics_cannot_turn_a_completed_submission_into_retry(self):
        self.engine.performance.directory = self.folder / "private-image.png"  # existing file, not a directory
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assertEqual(self.job.items[0].stage, ItemStage.COMPLETED.value)
        self.assertEqual([kind for kind, _ in self.adapter.sent_records], ["IMAGE", "TEXT"])
