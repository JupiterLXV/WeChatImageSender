"""Handoff timing must remove idle delay without weakening upload evidence."""
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from tests.test_upload_barrier import VirtualClock, frame_status, mocked_windows_adapter
from wechat_sender.wechat_adapter import WindowsWeChatAdapter


class TestNameLatency(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.png"
        Image.new("RGB", (160, 120), (35, 110, 205)).save(self.path)
        self.chat = Image.new("RGB", (500, 400), (28, 28, 28))
        self.adapter = mocked_windows_adapter(self.chat, self.chat)
        self.clock = VirtualClock()

    def verify(self):
        with patch("wechat_sender.wechat_adapter.time.monotonic", self.clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", self.clock.sleep
        ):
            return self.adapter.verify_image_sent(self.path, timeout_sec=.5, stable_duration_sec=1.5)

    def test_grace_period_does_not_restart_an_already_quiet_interval(self):
        self.adapter.post_send_grace_sec = 2.5
        self.assertTrue(self.verify().is_upload_complete)
        self.assertGreaterEqual(self.clock.now, 2.5)
        self.assertLess(self.clock.now, 2.7)

    def test_slow_classifier_cannot_replace_four_fresh_captures(self):
        samples = []
        def evaluate(*args, **kwargs):
            samples.append(self.clock.now)
            self.clock.now += 2.0  # computation, not another camera observation
            return frame_status()
        self.adapter.verifier.evaluate.side_effect = evaluate
        self.assertTrue(self.verify().is_upload_complete)
        self.assertGreaterEqual(len(samples), 4)
        self.assertGreaterEqual(samples[-1] - samples[0], 1.5)

    def test_spinner_seen_during_grace_restarts_full_quiet_interval(self):
        samples = []
        def evaluate(*args, **kwargs):
            spinner = self.clock.now < .6
            samples.append((self.clock.now, spinner))
            return frame_status(spinner=spinner, complete=not spinner)
        self.adapter.verifier.evaluate.side_effect = evaluate
        self.assertTrue(self.verify().is_upload_complete)
        first_quiet = next(t for t, spinner in samples if not spinner)
        self.assertGreaterEqual(self.clock.now - first_quiet, 1.5 - 1e-9)

    def test_slow_fourth_analysis_requires_a_new_observation_after_quiet_time(self):
        samples = []
        def evaluate(*args, **kwargs):
            samples.append(self.clock.now)
            if len(samples) == 4:
                self.clock.now += 2.0
            return frame_status()
        self.adapter.verifier.evaluate.side_effect = evaluate
        self.assertTrue(self.verify().is_upload_complete)
        self.assertLess(samples[3] - samples[0], 1.5)
        self.assertGreaterEqual(len(samples), 5)
        self.assertGreaterEqual(samples[-1] - samples[0], 1.5)

    def test_uia_paste_has_one_cancellable_settling_wait(self):
        for is_text, seconds in ((False, .30), (True, .12)):
            with self.subTest(is_text=is_text):
                calls = []
                def send_keys(keys, **kwargs):
                    calls.append((keys, kwargs.get("waitTime")))
                self.adapter._assert_submission_foreground = Mock()
                self.adapter._wait_or_stop = Mock(return_value=False)
                with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
                    "sys.modules", {"uiautomation": SimpleNamespace(SendKeys=send_keys)}
                ), patch("wechat_sender.wechat_adapter._uia_thread_scope", return_value=nullcontext()):
                    WindowsWeChatAdapter._paste_and_submit(self.adapter, "ENTER", is_text=is_text)
                self.assertEqual(calls, [("{Ctrl}v", 0), ("{Enter}", .08)])
                self.adapter._wait_or_stop.assert_called_once_with(seconds)
                self.assertEqual(self.adapter._assert_submission_foreground.call_count, 2)

    def test_stop_during_single_paste_wait_prevents_enter(self):
        self.adapter._assert_submission_foreground = Mock()
        self.adapter._wait_or_stop = Mock(return_value=True)
        send_keys = Mock()
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
            "sys.modules", {"uiautomation": SimpleNamespace(SendKeys=send_keys)}
        ), patch("wechat_sender.wechat_adapter._uia_thread_scope", return_value=nullcontext()):
            with self.assertRaises(RuntimeError):
                WindowsWeChatAdapter._paste_and_submit(self.adapter, "ENTER", is_text=True)
        send_keys.assert_called_once_with("{Ctrl}v", waitTime=0)


if __name__ == "__main__":
    unittest.main()
