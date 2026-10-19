import threading
import unittest
from unittest.mock import Mock, call, patch

from sender.core import NeedsReview, Stopped
from sender.qt_input import click_point


class QtInputTests(unittest.TestCase):
    def api(self):
        return Mock(GetForegroundWindow=Mock(return_value=42),
                    SetCursorPos=Mock(return_value=1))

    def test_existing_qt_input_route_keeps_renderer_transparent(self):
        api = self.api()
        with patch('sender.qt_input.user32', api), \
                patch('sender.qt_input._point_owned_by', return_value=True), \
                patch('sender.qt_input._render_windows') as renders, \
                patch('sender.qt_input.time.sleep'):
            click_point(42, 7, 842.5, 335, right=True)
        renders.assert_not_called()
        api.SetWindowLongPtrW.assert_not_called()
        self.assertEqual(api.mouse_event.call_args_list, [call(8, 0, 0, 0, 0), call(16, 0, 0, 0, 0)])

    def test_foreign_window_blocks_input(self):
        api = self.api()
        with patch('sender.qt_input.user32', api), \
                patch('sender.qt_input._point_owned_by', return_value=False), \
                patch('sender.qt_input._render_windows', return_value=[]):
            with self.assertRaises(NeedsReview):
                click_point(42, 7, 100, 100)
        api.SetCursorPos.assert_not_called()
        api.mouse_event.assert_not_called()

    def test_renderer_fallback_is_restored(self):
        api = self.api()
        api.GetWindowLongPtrW.return_value = 0x20
        with patch('sender.qt_input.user32', api), \
                patch('sender.qt_input._point_owned_by', side_effect=[False, True]), \
                patch('sender.qt_input._render_windows', return_value=[51]), \
                patch('sender.qt_input.time.sleep'):
            click_point(42, 7, 100, 100)
        self.assertEqual(api.SetWindowLongPtrW.call_args_list,
                         [call(51, -20, 0), call(51, -20, 0x20)])

    def test_focus_change_restores_style_without_clicking(self):
        api = self.api()
        api.GetForegroundWindow.side_effect = [42, 99]
        api.GetWindowLongPtrW.return_value = 0x20
        with patch('sender.qt_input.user32', api), \
                patch('sender.qt_input._point_owned_by', side_effect=[False, True]), \
                patch('sender.qt_input._render_windows', return_value=[51]):
            with self.assertRaises(NeedsReview):
                click_point(42, 7, 100, 100)
        api.SetWindowLongPtrW.assert_called_with(51, -20, 0x20)
        api.mouse_event.assert_not_called()

    def test_stop_or_cursor_failure_does_not_click(self):
        api = self.api()
        stop = threading.Event()
        stop.set()
        with patch('sender.qt_input.user32', api), \
                patch('sender.qt_input._point_owned_by', return_value=True):
            with self.assertRaises(Stopped):
                click_point(42, 7, 100, 100, stop=stop)
            api.SetCursorPos.return_value = 0
            with self.assertRaises(NeedsReview):
                click_point(42, 7, 100, 100)
        api.mouse_event.assert_not_called()
