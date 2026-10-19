import unittest

from sender.core import State
from sender.qt_status import Signal, message_state


class QtStatusTests(unittest.TestCase):
    def test_only_explicit_status_confirms_image(self):
        self.assertEqual(message_state([Signal('ListItemControl', '[图片]', '已发送', is_message=True)]), State.SENT)
        self.assertEqual(message_state([Signal('StatusBarControl', '发送完成')]), State.SENT)

    def test_thumbnail_and_unnamed_button_are_unknown(self):
        self.assertEqual(message_state([Signal('ListItemControl', '[图片]', is_message=True),
                                        Signal('ButtonControl')]), State.UNKNOWN)

    def test_disappearing_indicator_does_not_imply_completion(self):
        self.assertEqual(message_state([Signal('ProgressBarControl')]), State.SENDING)
        self.assertEqual(message_state([]), State.UNKNOWN)

    def test_failure_and_progress_override_success(self):
        sent = Signal('ListItemControl', '[图片]', '已发送', is_message=True)
        self.assertEqual(message_state([sent, Signal('ProgressBarControl')]), State.SENDING)
        self.assertEqual(message_state([sent, Signal('ButtonControl', '重新发送')]), State.FAILED)

    def test_text_or_avatar_named_sent_is_not_status(self):
        for kind in ('TextControl', 'ButtonControl', 'PaneControl'):
            self.assertEqual(message_state([Signal(kind, '已发送', '已发送')]), State.UNKNOWN)

    def test_hidden_stale_success_is_not_accepted(self):
        self.assertEqual(message_state([Signal('StatusBarControl', '已发送', visible=False)]), State.UNKNOWN)


if __name__ == '__main__':
    unittest.main()
