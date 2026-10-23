# -*- coding: utf-8 -*-
"""离线回归：停止、崩溃恢复及发送证据；不会连接或操作真实微信。"""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from wechat_sender.image_utils import VisualBubbleVerifier, scan_image_folder
from wechat_sender.job_store import ItemStage, SequentialSendEngine
from wechat_sender.wechat_adapter import BaseWeChatAdapter, DemoWeChatAdapter, WindowsWeChatAdapter, _uia_thread_scope


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.01
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def offline_windows_adapter():
    """跳过真实适配器构造器的系统检查，所有系统操作必须显式替身。"""
    adapter = WindowsWeChatAdapter.__new__(WindowsWeChatAdapter)
    BaseWeChatAdapter.__init__(adapter)
    adapter.verifier = VisualBubbleVerifier()
    adapter.wal_tracker = Mock()
    adapter._pre_send_shot = None
    adapter._last_locked_bbox = None
    adapter._pre_send_wal = None
    adapter.post_send_grace_sec = 0.0
    adapter._hwnd = 123
    adapter._expected_chat = "测试好友"
    adapter._verification_context_ok = Mock(return_value=True)
    adapter._paste_and_submit = Mock()
    return adapter


class TestInterruptedQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        Image.new("RGB", (100, 80), (30, 110, 200)).save(self.folder / "01.png")
        self.items, _ = scan_image_folder(self.folder)

    def create_job(self, adapter):
        engine = SequentialSendEngine(adapter, jobs_dir=self.folder / "jobs")
        job = engine.create_job(self.folder, self.items, "文件传输助手")
        return engine, job

    def assert_persisted_manual(self, engine, job):
        self.assertEqual(job.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)
        loaded = engine.load_job(engine.jobs_dir / f"{job.job_id}.json")
        self.assertEqual(loaded.items[0].stage, ItemStage.NEEDS_MANUAL_VERIFY.value)

    def test_crash_after_name_submission_requires_manual_verification(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        engine, job = self.create_job(adapter)
        job.items[0].stage = ItemStage.NAME_SUBMITTED.value
        ok, _ = engine.run_job(job)
        self.assertFalse(ok)
        self.assertEqual(adapter.sent_records, [])
        self.assert_persisted_manual(engine, job)

    def test_image_submission_exception_pauses_and_restores_clipboard(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        adapter.clipboard.set_text("原始剪贴板")
        adapter.send_image = Mock(side_effect=RuntimeError("剪贴板被占用"))
        previous_callback = lambda: False
        adapter.set_stop_callback(previous_callback)
        engine, job = self.create_job(adapter)
        ok, message = engine.run_job(job)
        self.assertFalse(ok)
        self.assertIn("队列已暂停", message)
        self.assertEqual(adapter.clipboard.last_payload, "原始剪贴板")
        self.assertIs(adapter.stop_callback, previous_callback)
        self.assert_persisted_manual(engine, job)

    def test_name_submission_exception_does_not_retry_image(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        adapter.send_text_and_verify = Mock(side_effect=RuntimeError("UIA 断开"))
        engine, job = self.create_job(adapter)
        ok, _ = engine.run_job(job)
        self.assertFalse(ok)
        self.assertEqual(adapter.sent_records, [("IMAGE", "01.png")])
        self.assert_persisted_manual(engine, job)
        ok_again, _ = engine.run_job(job)
        self.assertFalse(ok_again)
        self.assertEqual(len(adapter.sent_records), 1)

    def test_stop_during_image_confirmation_sends_no_name(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        engine, job = self.create_job(adapter)
        original_verify = adapter.verify_image_sent

        def stop_while_verifying(*args, **kwargs):
            engine.stop_requested = True
            return original_verify(*args, **kwargs)

        adapter.verify_image_sent = stop_while_verifying
        ok, _ = engine.run_job(job)
        self.assertFalse(ok)
        self.assertEqual(adapter.sent_records, [("IMAGE", "01.png")])
        self.assert_persisted_manual(engine, job)

    def test_exception_before_submission_preserves_pending_stage(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        adapter.ensure_foreground_and_target = Mock(side_effect=RuntimeError("窗口不可用"))
        engine, job = self.create_job(adapter)
        ok, _ = engine.run_job(job)
        self.assertFalse(ok)
        self.assertEqual(job.items[0].stage, ItemStage.PENDING.value)
        self.assertEqual(adapter.sent_records, [])

    def test_gui_stop_before_worker_entry_is_preserved(self):
        adapter = DemoWeChatAdapter(simulate_fast_ms=0)
        engine, job = self.create_job(adapter)
        engine.stop_requested = True
        ok, _ = engine.run_job(job, reset_stop=False)
        self.assertFalse(ok)
        self.assertEqual(adapter.sent_records, [])
        self.assertEqual(job.items[0].stage, ItemStage.PENDING.value)


class TestImageEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        y, x = np.mgrid[0:160, 0:200]
        pixels = np.stack((x * 255 // 200, y * 255 // 160, (x + y) * 127 // 360), axis=2).astype(np.uint8)
        self.source = Image.fromarray(pixels)
        self.path = Path(self.tmp.name) / "gradient.png"
        self.source.save(self.path)
        self.blank = Image.new("RGB", (500, 400), (30, 30, 30))
        self.chat = self.blank.copy()
        self.chat.paste(self.source.resize((160, 128)), (300, 200))

    def verify(self, adapter):
        clock = FakeClock()
        with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", clock.sleep
        ):
            return adapter.verify_image_sent(self.path, timeout_sec=0.65, stable_duration_sec=0.10)

    def test_adapter_never_promotes_frozen_spinner_to_success(self):
        ImageDraw.Draw(self.chat).ellipse((266, 254, 284, 272), outline=(210, 210, 210), width=3)
        adapter = offline_windows_adapter()
        adapter._pre_send_shot = self.blank
        adapter._capture_chat_region = Mock(return_value=self.chat)
        result = self.verify(adapter)
        self.assertTrue(result.matched)
        self.assertTrue(result.has_static_spinner)
        self.assertFalse(result.is_upload_complete)

    def test_unchanged_historical_image_cannot_confirm_new_send(self):
        adapter = offline_windows_adapter()
        adapter._pre_send_shot = self.chat.copy()
        adapter._capture_chat_region = Mock(return_value=self.chat)
        result = self.verify(adapter)
        self.assertTrue(result.matched)
        self.assertFalse(result.is_upload_complete)

    def test_new_quiet_outgoing_image_can_complete(self):
        adapter = offline_windows_adapter()
        adapter._pre_send_shot = self.blank
        adapter._capture_chat_region = Mock(return_value=self.chat)
        self.assertTrue(self.verify(adapter).is_upload_complete)

    def test_history_image_moving_up_after_unrelated_message_is_unconfirmed(self):
        adapter = offline_windows_adapter()
        adapter._pre_send_shot = self.chat.copy()
        moved = self.blank.copy()
        moved.paste(self.source.resize((160, 128)), (300, 110))
        adapter._capture_chat_region = Mock(return_value=moved)
        self.assertFalse(self.verify(adapter).is_upload_complete)

    def test_image_verification_stop_does_not_wait_for_timeout(self):
        adapter = offline_windows_adapter()
        adapter.set_stop_callback(lambda: True)
        adapter._capture_chat_region = Mock(side_effect=AssertionError("暂停后不应继续抓图"))
        self.assertFalse(self.verify(adapter).is_upload_complete)
        adapter._capture_chat_region.assert_not_called()

    def test_weak_similarity_and_unrelated_frame_change_never_promoted(self):
        adapter = offline_windows_adapter()
        adapter._pre_send_shot = self.blank
        adapter._capture_chat_region = Mock(return_value=self.chat)
        original = adapter.verifier.evaluate

        def weak_match(*args, **kwargs):
            status = original(*args, **kwargs)
            status.similarity = 0.30
            status.matched = False
            status.bbox = None
            status.is_upload_complete = False
            return status

        adapter.verifier.evaluate = weak_match
        self.assertFalse(self.verify(adapter).is_upload_complete)


class TestTextEvidenceAndTarget(unittest.TestCase):
    def verify_text(self, adapter):
        clock = FakeClock()
        with patch("wechat_sender.wechat_adapter.time.monotonic", clock.monotonic), patch(
            "wechat_sender.wechat_adapter.time.sleep", clock.sleep
        ):
            return adapter.send_text_and_verify("01_风景", "ENTER")

    def test_text_requires_new_matching_outgoing_message(self):
        adapter = offline_windows_adapter()
        adapter._count_outgoing_text_matches = Mock(side_effect=[0, 1])
        ok, _ = self.verify_text(adapter)
        self.assertTrue(ok)
        adapter._paste_and_submit.assert_called_once()

    def test_text_never_confirms_by_time_wal_or_unrelated_pixels(self):
        adapter = offline_windows_adapter()
        adapter._count_outgoing_text_matches = Mock(return_value=1)
        adapter.wal_tracker.snapshot.side_effect = AssertionError("WAL 不能证明文本发送")
        adapter._capture_chat_region = Mock(side_effect=AssertionError("任意画面变化不能证明文本发送"))
        ok, _ = self.verify_text(adapter)
        self.assertFalse(ok)

    def test_missing_message_accessibility_does_not_submit_text(self):
        adapter = offline_windows_adapter()
        adapter._count_outgoing_text_matches = Mock(return_value=None)
        ok, message = self.verify_text(adapter)
        self.assertFalse(ok)
        self.assertIn("未提交名称", message)
        adapter._paste_and_submit.assert_not_called()

    def test_cancellation_during_text_confirmation_is_not_success(self):
        adapter = offline_windows_adapter()
        adapter._count_outgoing_text_matches = Mock(return_value=0)
        adapter._paste_and_submit.side_effect = lambda *a, **kw: adapter.set_stop_callback(lambda: True)
        self.assertFalse(self.verify_text(adapter)[0])

    def check_target(self, current, expected):
        adapter = offline_windows_adapter()
        adapter._find_wechat_hwnd = Mock(return_value=123)
        adapter.get_current_chat_title = Mock(return_value=current)
        fake_user32 = SimpleNamespace(GetForegroundWindow=lambda: 123)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch(
            "wechat_sender.wechat_adapter.ctypes.windll", SimpleNamespace(user32=fake_user32)
        ):
            return adapter.ensure_foreground_and_target(expected)

    def test_generic_or_missing_chat_title_fails_closed(self):
        for current in ("", "微信", "当前微信聊天"):
            with self.subTest(current=current):
                self.assertFalse(self.check_target(current, "测试好友")[0])

    def test_substring_target_match_is_rejected(self):
        self.assertFalse(self.check_target("测试好友备份群", "测试好友")[0])
        self.assertFalse(self.check_target("测试好友", "测试好友备份群")[0])
        self.assertTrue(self.check_target("测试好友", "测试好友")[0])

    def test_generic_target_is_rejected(self):
        for target in ("", "微信", "当前微信聊天"):
            self.assertFalse(self.check_target("测试好友", target)[0])


class TestAccessibilityEvidence(unittest.TestCase):
    @staticmethod
    def node(kind, name, box, children=()):
        return SimpleNamespace(
            ControlTypeName=kind,
            Name=name,
            BoundingRectangle=SimpleNamespace(left=box[0], top=box[1], right=box[2], bottom=box[3]),
            GetChildren=lambda: list(children),
        )

    def test_text_snapshot_excludes_incoming_rows_and_drafts(self):
        incoming = self.node("TextControl", "01_风景", (310, 400, 510, 430))
        outgoing = self.node("TextControl", "01_风景", (760, 500, 960, 530))
        # Full-width UIA list rows do not establish which side sent a message.
        broad_row = self.node("ListItemControl", "01_风景", (280, 450, 990, 480))
        draft = self.node("EditControl", "01_风景", (760, 660, 960, 700))
        area = self.node("ListControl", "消息", (280, 130, 990, 640), [incoming, outgoing, broad_row])
        root = self.node("WindowControl", "微信", (0, 0, 1000, 800), [area, draft])
        adapter = offline_windows_adapter()
        adapter._find_wechat_hwnd = Mock(return_value=123)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
            "sys.modules", {"uiautomation": SimpleNamespace(ControlFromHandle=lambda _: root)}
        ):
            self.assertEqual(adapter._count_outgoing_text_matches("01_风景"), 1)
            self.assertEqual(adapter._count_outgoing_text_matches("其他名称"), 0)

    def test_chat_identity_comes_from_header_not_edit_name(self):
        draft = self.node("EditControl", "危险的草稿内容", (350, 660, 950, 700))
        heading = self.node("TextControl", "测试好友", (350, 60, 650, 86))
        root = self.node("WindowControl", "微信", (0, 0, 1000, 800), [draft, heading])
        adapter = offline_windows_adapter()
        adapter._find_wechat_hwnd = Mock(return_value=123)

        def get_window_text(_hwnd, buf, _size):
            buf.value = "微信"
            return 2

        fake_user32 = SimpleNamespace(GetWindowTextW=get_window_text)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
            "sys.modules", {"uiautomation": SimpleNamespace(ControlFromHandle=lambda _: root)}
        ), patch("wechat_sender.wechat_adapter.ctypes.windll", SimpleNamespace(user32=fake_user32)):
            self.assertEqual(adapter.get_current_chat_title(), "测试好友")
            root.GetChildren = lambda: [draft]
            self.assertEqual(adapter.get_current_chat_title(), "")

    def test_uia_scope_propagates_callers_original_exception(self):
        fake_uia = SimpleNamespace(UIAutomationInitializerInThread=nullcontext)
        with patch("wechat_sender.wechat_adapter.sys.platform", "win32"), patch.dict(
            "sys.modules", {"uiautomation": fake_uia}
        ):
            with self.assertRaisesRegex(ValueError, "原始异常"):
                with _uia_thread_scope():
                    raise ValueError("原始异常")


if __name__ == "__main__":
    unittest.main()
