import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sender import accessibility as acc
from sender.core import NeedsReview
from sender.wechat import WeChatBackend


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.leases = patch.object(acc, '_leases', {})
        self.leases.start()
        self.addCleanup(self.leases.stop)

    def test_only_exact_binary_and_build_have_an_offset(self):
        ((version, digest), offset), = acc.VERIFIED_BUILDS.items()
        self.assertEqual(acc.activation_offset(version, digest), offset)
        for bad_version, bad_digest in [('4.1.15.54', digest), (version, '0' * 64)]:
            with self.assertRaises(NeedsReview):
                acc.activation_offset(bad_version, bad_digest)

    def test_unknown_binary_is_rejected_before_opening_for_write(self):
        with patch.object(acc, '_module', return_value=(100, 100, Path('mock.dll'))), \
                patch.object(Path, 'read_bytes', return_value=b'unknown'), \
                patch.object(acc, '_kernel') as kernel:
            with self.assertRaises(NeedsReview):
                acc.enable(123, 456, '4.1.15.53')
            kernel.OpenProcess.assert_not_called()

    def activate_with(self, original, present):
        patches = [patch.object(acc, '_module', return_value=(100, 100, Path('mock.dll'))),
                   patch.object(Path, 'read_bytes', return_value=b'fixture'),
                   patch.object(acc, 'activation_offset', return_value=10),
                   patch.object(acc, '_kernel'), patch.object(acc, '_read_byte', return_value=original),
                   patch.object(acc, '_write_byte'), patch.object(acc, '_present', side_effect=present)]
        values = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        values[3].OpenProcess.return_value = 789
        return values[3], values[5]

    def test_changed_flag_is_leased_and_restored(self):
        kernel, write = self.activate_with(0, [True])
        acc.enable(123, 456, '4.1.15.53')
        write.assert_called_once_with(789, 110, 1)
        kernel.CloseHandle.assert_not_called()
        with patch.object(acc, '_read_byte', return_value=1):
            acc.restore_all()
        self.assertEqual(write.call_args_list[-1].args, (789, 110, 0))
        kernel.CloseHandle.assert_called_once_with(789)
        self.assertFalse(acc._leases)

    def test_provider_failure_restores_flag_and_handle(self):
        kernel, write = self.activate_with(0, RuntimeError('provider failed'))
        with self.assertRaises(RuntimeError):
            acc.enable(123, 456, '4.1.15.53')
        self.assertEqual([call.args for call in write.call_args_list], [(789, 110, 1), (789, 110, 0)])
        kernel.CloseHandle.assert_called_once_with(789)

    def test_preexisting_active_flag_is_never_restored_to_zero(self):
        kernel, write = self.activate_with(1, [True])
        acc.enable(123, 456, '4.1.15.53')
        acc.restore_all()
        write.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(789)

    def test_non_boolean_value_is_never_modified(self):
        kernel, write = self.activate_with(17, [True])
        with self.assertRaises(NeedsReview):
            acc.enable(123, 456, '4.1.15.53')
        write.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(789)

    def test_backend_does_not_enable_flag_without_consent(self):
        backend = WeChatBackend()
        window = Mock(ClassName='mmui::MainWindow', NativeWindowHandle=123, ProcessId=456)
        with patch.object(backend, '_windows', return_value=[(window, 'Weixin.exe', '4.1.15.53')]), \
                patch('sender.wechat.walk', return_value=[window]), patch.object(acc, 'enable') as enable:
            backend.connect()
            enable.assert_not_called()


if __name__ == '__main__':
    unittest.main()
