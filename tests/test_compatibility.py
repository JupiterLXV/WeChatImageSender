import unittest

from sender.compatibility import aid_matches, check_version, editor_chat_candidates


class CompatibilityTests(unittest.TestCase):
    def test_target_patch_builds_are_supported(self):
        for version in ('4.1.15', '4.1.15.13', '4.1.15.53'):
            self.assertTrue(check_version(version).supported)

    def test_old_version_requires_wechat_update(self):
        for version in ('3.9.12.55', '4.1.12.26', '4.1.14'):
            report = check_version(version)
            self.assertFalse(report.supported)
            self.assertIn('将微信更新', report.message)

    def test_future_version_requires_app_update(self):
        for version in ('4.1.16.1', '4.2.0', '5.0.0'):
            report = check_version(version)
            self.assertFalse(report.supported)
            self.assertIn('更新发送助手', report.message)

    def test_unknown_and_prefix_matches_are_rejected(self):
        for version in ('', '未知', '4.1.150', '4.1.15beta', '4.1.15.1.2'):
            self.assertFalse(check_version(version).supported)

    def test_automation_ids_require_exact_last_segment(self):
        self.assertTrue(aid_matches('chat_input_field', 'chat_input_field'))
        self.assertTrue(aid_matches('MainView.chat.chat_input_field', 'chat_input_field'))
        self.assertFalse(aid_matches('other_chat_input_field', 'chat_input_field'))
        self.assertFalse(aid_matches('chat_input_field.button', 'chat_input_field'))

    def test_chat_hint_is_only_a_candidate_not_an_identity(self):
        name = '文件传输助手按住鼠标 语音输入文字'
        self.assertEqual(editor_chat_candidates(name), {name, '文件传输助手', '文件传输助手按住鼠标'})
        self.assertEqual(editor_chat_candidates('测试群'), {'测试群'})
        for suffix in ('长按鼠标 语音输入', '长按鼠标语音输入文字', '按住鼠标 语音输入'):
            full_name = '文件传输助手' + suffix
            self.assertIn('文件传输助手', editor_chat_candidates(full_name))
            self.assertIn(full_name, editor_chat_candidates(full_name))


if __name__ == '__main__':
    unittest.main()
