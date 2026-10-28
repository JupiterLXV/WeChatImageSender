"""Diagnostic failures must never become a message retry or skip cleanup."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from wechat_sender.clipboard_win32 import ClipboardBusyError
from wechat_sender.image_utils import scan_image_folder
from wechat_sender.job_store import ItemStage, SequentialSendEngine
from wechat_sender.performance import ItemPerformanceTrace
from wechat_sender.wechat_adapter import (BaseWeChatAdapter, DemoWeChatAdapter,
                                           MessagePreparationError, WindowsWeChatAdapter)


class TestTraceFailureBoundaries(unittest.TestCase):
    def test_clock_failure_on_entry_runs_business_and_preserves_exception(self):
        trace = ItemPerformanceTrace(0)
        trace.clock = Mock(side_effect=RuntimeError("synthetic diagnostic clock"))
        called = []
        with trace.measure("capture"):
            called.append(True)
        self.assertEqual(called, [True])
        with self.assertRaisesRegex(ValueError, "business failure"):
            with trace.measure("capture"):
                raise ValueError("business failure")

    def test_clock_failure_on_exit_preserves_business_exception_and_stack(self):
        trace = ItemPerformanceTrace(0)
        trace.clock = Mock(side_effect=[1.0, RuntimeError("synthetic diagnostic clock")])
        with self.assertRaisesRegex(ValueError, "business failure"):
            with trace.measure("capture"):
                raise ValueError("business failure")
        self.assertEqual(trace._stack, [])

    def test_stage_bookkeeping_failure_does_not_replace_business_exception(self):
        trace = ItemPerformanceTrace(0)
        trace.stages = Mock()
        trace.stages.setdefault.side_effect = RuntimeError("synthetic stage storage")
        with self.assertRaisesRegex(ValueError, "business failure"):
            with trace.measure("capture"):
                raise ValueError("business failure")
        self.assertEqual(trace._stack, [])


class TestEngineDiagnosticFailureBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        Image.new("RGB", (60, 40), (40, 110, 200)).save(self.folder / "01.png")
        items, _ = scan_image_folder(self.folder)
        self.adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        self.adapter.activate_chat("synthetic chat")
        self.adapter.clipboard.set_text("synthetic original clipboard")
        self.old_stop = lambda: False
        self.old_trace = object()
        self.adapter.set_stop_callback(self.old_stop)
        self.adapter.performance_trace = self.old_trace
        self.engine = SequentialSendEngine(self.adapter, jobs_dir=self.folder / "jobs")
        # No instrumentation is active while creating the persisted task.
        self.job = self.engine.create_job(self.folder, items, "synthetic chat")

    def assert_cleanup(self):
        self.assertIs(self.adapter.stop_callback, self.old_stop)
        self.assertIs(self.adapter.performance_trace, self.old_trace)
        self.assertEqual(self.adapter.clipboard.last_payload, "synthetic original clipboard")
        self.assertIsNone(self.engine._active_trace)

    def assert_single_completed_pair(self):
        self.assertEqual(self.job.items[0].stage, ItemStage.COMPLETED.value)
        self.assertEqual([kind for kind, _ in self.adapter.sent_records], ["IMAGE", "TEXT"])
        self.assert_cleanup()

    def test_trace_constructor_failure_only_disables_diagnostics(self):
        with patch("wechat_sender.job_store.ItemPerformanceTrace", side_effect=RuntimeError("synthetic clock")):
            self.assertTrue(self.engine.run_job(self.job)[0])
        self.assert_single_completed_pair()

    def test_clock_failure_throughout_run_does_not_change_business(self):
        trace = ItemPerformanceTrace(0)
        trace.clock = Mock(side_effect=RuntimeError("synthetic clock"))
        with patch("wechat_sender.job_store.ItemPerformanceTrace", return_value=trace):
            self.assertTrue(self.engine.run_job(self.job)[0])
        self.assert_single_completed_pair()

    def test_counter_read_failure_at_start_only_disables_diagnostics(self):
        self.engine._read_performance_counters = Mock(side_effect=RuntimeError("synthetic counters"))
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assert_single_completed_pair()

    def test_counter_read_failure_at_finish_does_not_skip_cleanup(self):
        self.engine._read_performance_counters = Mock(side_effect=[{}, RuntimeError("synthetic counters")])
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assert_single_completed_pair()

    def test_finish_failure_does_not_skip_cleanup_or_change_result(self):
        self.engine.performance.finish = Mock(side_effect=RuntimeError("synthetic diagnostics write"))
        self.assertTrue(self.engine.run_job(self.job)[0])
        self.assert_single_completed_pair()

    def test_retry_counter_failure_does_not_prevent_safe_second_attempt(self):
        trace = ItemPerformanceTrace(0)
        trace.add_counters = Mock(side_effect=RuntimeError("synthetic diagnostic counters"))
        original = self.adapter.send_image
        calls = []
        def transient(*args):
            calls.append(True)
            if len(calls) == 1:
                raise MessagePreparationError("synthetic busy clipboard", retryable=True)
            return original(*args)
        self.adapter.send_image = transient
        with patch("wechat_sender.job_store.ItemPerformanceTrace", return_value=trace):
            self.assertTrue(self.engine.run_job(self.job)[0])
        self.assertEqual(len(calls), 2)
        self.assert_single_completed_pair()

    def test_finish_failure_keeps_unknown_submission_manual_without_replay(self):
        self.engine.performance.finish = Mock(side_effect=RuntimeError("synthetic diagnostics write"))
        self.adapter.send_image = Mock(side_effect=RuntimeError("synthetic paste may have happened"))
        self.assertFalse(self.engine.run_job(self.job)[0])
        self.assertEqual(self.adapter.send_image.call_count, 1)
        self.assertEqual(self.job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)
        self.assertEqual(self.adapter.sent_records, [])
        self.assert_cleanup()


class TestAdapterPreparationBoundaries(unittest.TestCase):
    def setUp(self):
        # Skip Windows initialization; every desktop and clipboard operation
        # below is replaced before the real adapter methods can be called.
        self.adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
        BaseWeChatAdapter.__init__(self.adapter)
        self.adapter._hwnd = 123
        self.adapter._capture_chat_region = Mock(return_value=Image.new("RGB", (80, 60)))
        self.adapter._paste_and_submit = Mock()
        self.adapter.clipboard = Mock()

    def test_image_clipboard_busy_is_retryable_before_any_paste(self):
        self.adapter.clipboard.set_image_file_hdrop.side_effect = ClipboardBusyError("synthetic busy")
        with self.assertRaises(MessagePreparationError) as caught:
            self.adapter.send_image(Path("synthetic.png"), "CF_HDROP", "ENTER")
        self.assertTrue(caught.exception.retryable)
        self.adapter._paste_and_submit.assert_not_called()

    def test_capture_failure_is_not_retried_and_never_prepares_clipboard(self):
        self.adapter._capture_chat_region.side_effect = RuntimeError("synthetic unavailable frame")
        with self.assertRaises(MessagePreparationError) as caught:
            self.adapter.send_image(Path("synthetic.png"), "CF_HDROP", "ENTER")
        self.assertFalse(caught.exception.retryable)
        self.adapter.clipboard.set_image_file_hdrop.assert_not_called()
        self.adapter._paste_and_submit.assert_not_called()

    def test_image_paste_exception_is_never_reclassified_as_preparation(self):
        error = RuntimeError("synthetic partial paste")
        self.adapter._paste_and_submit.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.send_image(Path("synthetic.png"), "CF_HDROP", "ENTER")
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, MessagePreparationError)
        self.adapter._paste_and_submit.assert_called_once()

    def test_name_clipboard_busy_is_retryable_before_any_paste(self):
        self.adapter.clipboard.set_text.side_effect = ClipboardBusyError("synthetic busy")
        with self.assertRaises(MessagePreparationError) as caught:
            self.adapter.send_text_and_verify("synthetic", "ENTER")
        self.assertTrue(caught.exception.retryable)
        self.adapter._paste_and_submit.assert_not_called()

    def test_name_paste_exception_is_never_reclassified_as_preparation(self):
        error = RuntimeError("synthetic partial paste")
        self.adapter._paste_and_submit.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.send_text_and_verify("synthetic", "ENTER")
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, MessagePreparationError)
        self.adapter._paste_and_submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
