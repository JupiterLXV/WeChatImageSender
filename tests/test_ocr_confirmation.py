import unittest
from unittest.mock import Mock, patch
from PIL import Image
from wechat_sender.wechat_adapter import WindowsWeChatAdapter
from wechat_sender.windows_ocr import OcrLine


class TestOcrConfirmation(unittest.TestCase):
    def setUp(self):
        self.adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
        self.adapter._capture_ocr_area = Mock(return_value=Image.new('RGB', (600, 400), 'white'))

    def test_header_normalizes_chinese_ocr_spaces(self):
        with patch('wechat_sender.wechat_adapter.recognize_local_text', return_value=[OcrLine('文 件 传 输 助 手', (30, 20, 160, 24))]):
            self.assertEqual(self.adapter._ocr_chat_title(), '文件传输助手')

    def test_generic_window_text_does_not_identify_a_chat(self):
        with patch('wechat_sender.wechat_adapter.recognize_local_text', return_value=[OcrLine('微 信', (30, 10, 80, 24)), OcrLine('搜索', (10, 40, 40, 20))]):
            self.assertEqual(self.adapter._ocr_chat_title(), '')

    def test_matching_incoming_text_does_not_count_as_outgoing(self):
        with patch('wechat_sender.wechat_adapter.recognize_local_text', return_value=[OcrLine('发送测试01', (20, 90, 130, 25))]):
            self.assertEqual(self.adapter._count_outgoing_text_ocr('发送测试01'), 0)

    def test_outgoing_name_requires_exact_normalized_text(self):
        lines = [OcrLine('发 送 测 试 01', (420, 220, 130, 24)), OcrLine('发送测试010', (420, 280, 140, 24))]
        with patch('wechat_sender.wechat_adapter.recognize_local_text', return_value=lines):
            self.assertEqual(self.adapter._count_outgoing_text_ocr('发送测试01'), 1)

    def test_missing_ocr_capability_is_unknown_not_zero_messages(self):
        with patch('wechat_sender.wechat_adapter.recognize_local_text', return_value=None):
            self.assertIsNone(self.adapter._count_outgoing_text_ocr('发送测试01'))

    def test_small_text_refinement_must_still_match_complete_name(self):
        base = [OcrLine('发氵则试OI', (420, 220, 130, 24))]
        with patch('wechat_sender.wechat_adapter.recognize_local_text', side_effect=[base, [OcrLine('发 送 测 试 01', (1, 1, 260, 48))]]):
            self.assertEqual(self.adapter._count_outgoing_text_ocr('发送测试01'), 1)
        with patch('wechat_sender.wechat_adapter.recognize_local_text', side_effect=[base, [OcrLine('发送测试010', (1, 1, 260, 48))], [OcrLine('发送测试010', (1, 1, 390, 72))]]):
            self.assertEqual(self.adapter._count_outgoing_text_ocr('发送测试01'), 0)

    def test_wider_crop_refinement_requires_full_exact_name(self):
        base = [OcrLine('发氵则试O2', (420, 220, 130, 24))]
        with patch('wechat_sender.wechat_adapter.recognize_local_text', side_effect=[
            base, [OcrLine('发送测试O2', (1, 1, 260, 48))],
            [OcrLine('发送测试02', (1, 1, 390, 72))],
        ]) as recognize:
            self.assertEqual(self.adapter._count_outgoing_text_ocr('发送测试02'), 1)
            self.assertEqual(recognize.call_args_list[1].args[0].size, (268, 56))
            self.assertEqual(recognize.call_args_list[2].args[0].size, (420, 102))

    def test_no_foreground_capture_cannot_confirm_target(self):
        self.adapter._capture_ocr_area.return_value = None
        self.assertEqual(self.adapter._ocr_chat_title(), '')
        self.assertIsNone(self.adapter._count_outgoing_text_ocr('发送测试01'))
