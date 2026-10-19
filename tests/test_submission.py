import threading
import unittest
from unittest.mock import Mock

from sender.core import NeedsReview, SenderError, Stopped
from sender.wechat import WeChatBackend


class SubmissionTests(unittest.TestCase):
    def backend(self):
        backend = WeChatBackend()
        backend.editor = Mock()
        backend.editor.HasKeyboardFocus = True
        return backend

    def test_submits_enter_once_without_clicking_other_controls(self):
        backend = self.backend()
        backend._invoke_send()
        backend.editor.SendKeys.assert_called_once_with('{Enter}', waitTime=0)

    def test_uses_configured_ctrl_enter_once(self):
        backend = self.backend()
        backend.send_shortcut = 'Ctrl+Enter'
        backend._invoke_send()
        backend.editor.SendKeys.assert_called_once_with('{Ctrl}{Enter}', waitTime=0)

    def test_missing_focus_stops_before_keystroke(self):
        backend = self.backend()
        backend.editor.HasKeyboardFocus = False
        with self.assertRaises(NeedsReview):
            backend._invoke_send()
        backend.editor.SendKeys.assert_not_called()

    def test_stop_and_unknown_shortcut_do_not_submit(self):
        backend = self.backend()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Stopped):
            backend._invoke_send(stop)
        backend.editor.SendKeys.assert_not_called()
        backend.send_shortcut = 'other'
        with self.assertRaises(SenderError):
            backend._invoke_send()
        backend.editor.SendKeys.assert_not_called()

    def test_uncertain_keystroke_is_never_retried(self):
        backend = self.backend()
        backend.editor.SendKeys.side_effect = RuntimeError('provider stopped responding')
        with self.assertRaises(RuntimeError):
            backend._invoke_send()
        self.assertEqual(backend.editor.SendKeys.call_count, 1)


if __name__ == '__main__':
    unittest.main()
