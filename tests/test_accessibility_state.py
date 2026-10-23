import unittest
from types import SimpleNamespace
from unittest.mock import patch
from wechat_sender.accessibility import ZeroPatchAccessibilityActivator, SPI_SETSCREENREADER


class TestRepeatedAccessibilityActivation(unittest.TestCase):
    def test_repeated_activation_restores_original_disabled_flag(self):
        calls = []
        user32 = SimpleNamespace(SystemParametersInfoW=lambda *args: calls.append(args))
        with patch('wechat_sender.accessibility.sys.platform', 'win32'), \
             patch('wechat_sender.accessibility.ctypes.windll', SimpleNamespace(user32=user32), create=True), \
             patch.object(ZeroPatchAccessibilityActivator, 'get_screen_reader_flag', side_effect=[False, True]):
            activator = ZeroPatchAccessibilityActivator()
            activator.activate()
            activator.activate()
            activator.restore()
        self.assertEqual([(call[0], call[1]) for call in calls], [(SPI_SETSCREENREADER, 1), (SPI_SETSCREENREADER, 0)])

    def test_existing_enabled_flag_remains_enabled(self):
        calls = []
        user32 = SimpleNamespace(SystemParametersInfoW=lambda *args: calls.append(args))
        with patch('wechat_sender.accessibility.sys.platform', 'win32'), \
             patch('wechat_sender.accessibility.ctypes.windll', SimpleNamespace(user32=user32), create=True), \
             patch.object(ZeroPatchAccessibilityActivator, 'get_screen_reader_flag', return_value=True):
            activator = ZeroPatchAccessibilityActivator()
            activator.activate()
            activator.activate()
            activator.restore()
        self.assertEqual(calls, [])
