import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sender.core import NeedsReview, State
from sender.qt_menu import completed_image_menu, image_menu_point, read_image_menu


def menu(names):
    return [{'name': name, 'enabled': True} for name in names]


COMPLETE = ['复制', '编辑', '转发...', '收藏', '多选', '提醒', '引用', '用窗口打开', '另存为...', '打开方式', '删除']
UPLOADING = ['转发...', '多选', '用窗口打开', '删除']


class MenuTests(unittest.TestCase):
    def test_menu_uses_the_matched_bounds(self):
        self.assertEqual(image_menu_point((695, 205, 1397, 378), (305, 11, 612, 159)),
                         (1153.5, 290))

    def test_missing_or_outside_match_cannot_open_menu(self):
        for bounds in (None, (-1, 11, 612, 159), (305, 11, 703, 159), (305, 11, 612, 174)):
            with self.assertRaises(NeedsReview):
                image_menu_point((695, 205, 1397, 378), bounds)

    def test_row_moved_after_capture_blocks_click(self):
        backend = Mock(stop=None, assert_target=Mock())
        row = Mock()
        row.GetRuntimeId.return_value = [42, 1]
        row.BoundingRectangle = SimpleNamespace(left=695, top=206, right=1397, bottom=379)
        with patch('sender.qt_menu._menus', return_value={}), patch('sender.qt_menu.click_point') as click:
            with self.assertRaisesRegex(NeedsReview, '位置'):
                read_image_menu(backend, {'pid': 1}, row, (305, 11, 612, 159), 120,
                                (695, 205, 1397, 378))
            click.assert_not_called()

    def test_measured_upload_menu_never_confirms(self):
        for state in State:
            self.assertFalse(completed_image_menu(menu(UPLOADING), state, '4.1.15.53'))

    def test_complete_menu_requires_clear_image_and_exact_build(self):
        self.assertTrue(completed_image_menu(menu(COMPLETE), State.CLEAR, '4.1.15.53'))
        for state in State:
            if state != State.CLEAR:
                self.assertFalse(completed_image_menu(menu(COMPLETE), state, '4.1.15.53'))
        self.assertFalse(completed_image_menu(menu(COMPLETE), State.CLEAR, '4.1.15.54'))

    def test_disabled_missing_or_duplicate_actions_are_rejected(self):
        for key in ['复制', '编辑', '收藏', '引用', '另存为...', '用窗口打开']:
            items = menu(COMPLETE)
            next(item for item in items if item['name'] == key)['enabled'] = False
            self.assertFalse(completed_image_menu(items, State.CLEAR, '4.1.15.53'))
            self.assertFalse(completed_image_menu(menu([x for x in COMPLETE if x != key]), State.CLEAR, '4.1.15.53'))
        self.assertFalse(completed_image_menu(menu(COMPLETE + ['复制']), State.CLEAR, '4.1.15.53'))

    def test_retry_or_cancel_overrides_complete_signature(self):
        for name in ['发送中', '取消发送', '重新发送', '重发', '发送失败']:
            self.assertFalse(completed_image_menu(menu(COMPLETE + [name]), State.CLEAR, '4.1.15.53'))


if __name__ == '__main__':
    unittest.main()
