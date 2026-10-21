# -*- coding: utf-8 -*-
"""
WeChatImageSender v3.2.0 完整离线单元测试套件：
覆盖自然排序、MPO双帧主图提取、动图拦截、透明通道转白底、CF_HDROP 结构体编码、
默认阈值 0.40、OpenCV 中心 80% 多尺度定位（含微信圆角遮罩与非标缩放宽度）、
【新增】弱网静态卡死转圈图标一票否决（Static Spinner Veto，两帧差分==0 仍能拦截）、
红色失败标记识别、上传转圈帧间差分检测、队列状态机与人工恢复决策。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from wechat_sender.accessibility import ZeroPatchAccessibilityActivator
from wechat_sender.clipboard_win32 import ClipboardManager, build_cf_hdrop_payload
from wechat_sender.image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    VisualBubbleVerifier,
    image_to_dib_bytes,
    load_static_image,
    natural_sort_key,
    scan_image_folder,
)
from wechat_sender.job_store import (
    ItemStage,
    ManualDecision,
    SequentialSendEngine,
)
from wechat_sender.wechat_adapter import DemoWeChatAdapter


class TestImageUtilsAndSorting(unittest.TestCase):
    def test_default_threshold_is_0_40(self) -> None:
        self.assertAlmostEqual(DEFAULT_SIMILARITY_THRESHOLD, 0.40)
        v = VisualBubbleVerifier()
        self.assertAlmostEqual(v.similarity_threshold, 0.40)

    def test_natural_sort_order(self) -> None:
        names = ["10.png", "2.png", "1.png", "20.png", "a2.jpg", "a10.jpg"]
        sorted_names = sorted(names, key=natural_sort_key)
        self.assertEqual(sorted_names, ["1.png", "2.png", "10.png", "20.png", "a2.jpg", "a10.jpg"])

    def test_transparency_to_white_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "transparent.png"
            rgba = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
            rgba.save(p, format="PNG")
            rgb, fmt, is_mpo = load_static_image(p)
            self.assertEqual(fmt, "PNG")
            self.assertFalse(is_mpo)
            self.assertEqual(rgb.mode, "RGB")
            self.assertEqual(rgb.getpixel((10, 10)), (255, 255, 255))

    def test_animated_gif_rejected_in_dib_mode_but_allowed_in_hdrop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "anim.gif"
            f1 = Image.new("RGB", (40, 40), (255, 0, 0))
            f2 = Image.new("RGB", (40, 40), (0, 255, 0))
            f1.save(p, save_all=True, append_images=[f2], duration=100, loop=0)

            with self.assertRaises(ValueError):
                load_static_image(p)

            valid_dib, skipped_dib = scan_image_folder(Path(tmp), allow_animated_in_file_mode=False)
            self.assertEqual(len(valid_dib), 0)
            self.assertEqual(len(skipped_dib), 1)

            valid_hdrop, skipped_hdrop = scan_image_folder(Path(tmp), allow_animated_in_file_mode=True)
            self.assertEqual(len(valid_hdrop), 1)
            self.assertEqual(len(skipped_hdrop), 0)

    def test_dib_header_strip(self) -> None:
        img = Image.new("RGB", (32, 32), (10, 20, 30))
        dib = image_to_dib_bytes(img)
        bi_size = struct.unpack("<I", dib[:4])[0]
        self.assertEqual(bi_size, 40)


class TestClipboardPayload(unittest.TestCase):
    def test_cf_hdrop_binary_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "photo_01.jpg"
            f.write_bytes(b"dummy")
            payload = build_cf_hdrop_payload(f)
            p_files, pt_x, pt_y, f_nc, f_wide = struct.unpack("<IiiII", payload[:20])
            self.assertEqual(p_files, 20)
            self.assertEqual((pt_x, pt_y, f_nc), (0, 0, 0))
            self.assertEqual(f_wide, 1)
            decoded_path = payload[20:-4].decode("utf-16le")
            self.assertEqual(decoded_path, str(f.resolve()))

    def test_clipboard_manager_dry_run(self) -> None:
        mgr = ClipboardManager(dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "1.png"
            Image.new("RGB", (50, 50), (12, 34, 56)).save(p)
            mgr.set_image_file_hdrop(p)
            self.assertEqual(mgr.last_mode, "CF_HDROP")
            mgr.set_image_dib(p)
            self.assertEqual(mgr.last_mode, "CF_DIB")
            mgr.set_text("测试名称")
            self.assertEqual(mgr.last_mode, "CF_UNICODETEXT")
            self.assertEqual(mgr.last_payload, "测试名称")


class TestVisualBubbleVerifier(unittest.TestCase):
    def _create_gradient_source(self, w: int = 240, h: int = 160) -> Image.Image:
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        for y in range(h):
            for x in range(w):
                arr[y, x] = [(x * 255) // w, (y * 255) // h, ((x + y) * 127) // (w + h)]
        return Image.fromarray(arr, mode="RGB")

    def test_multiscale_locate_and_quiet_confirm(self) -> None:
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(240, 150)

        chat_1 = Image.new("RGB", (500, 400), (30, 30, 30))
        thumb = src.resize((180, 112), Image.Resampling.BILINEAR)
        chat_1.paste(thumb, (283, 217))
        chat_2 = chat_1.copy()

        status = verifier.evaluate(chat_2, src, prev_screenshot=chat_1)
        self.assertTrue(status.matched)
        self.assertFalse(status.has_failure_badge)
        self.assertFalse(status.has_static_spinner)
        self.assertTrue(status.is_upload_complete)
        self.assertGreater(status.similarity, 0.90)

    def test_rounded_corners_and_nonstandard_scale_match(self) -> None:
        """测试微信真实气泡具有圆角遮罩、边框及非标缩放宽度 (如 153px) 时的鲁棒高分匹配。"""
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(320, 200)

        chat = Image.new("RGB", (520, 420), (28, 28, 28))
        thumb = src.resize((153, 96), Image.Resampling.BILINEAR)
        draw_t = ImageDraw.Draw(thumb)
        draw_t.rectangle((0, 0, 12, 12), fill=(28, 28, 28))
        draw_t.rectangle((140, 0, 153, 12), fill=(28, 28, 28))
        draw_t.rectangle((0, 84, 12, 96), fill=(28, 28, 28))
        draw_t.rectangle((140, 84, 153, 96), fill=(28, 28, 28))
        chat.paste(thumb, (315, 250))

        status = verifier.evaluate(chat, src, prev_screenshot=chat)
        self.assertTrue(status.matched)
        self.assertTrue(status.is_upload_complete)
        self.assertGreater(status.similarity, 0.80)

    def test_frozen_static_spinner_blocks_completion_even_when_zero_motion(self) -> None:
        """
        【核心回归测试】模拟弱网下转圈动画卡死不动（chat_1 与 chat_2 完全相同，帧间差分 motion == 0.0）：
        验证 detect_static_spinner 能够识别左侧状态区静态转圈圆环并一票否决，绝不误放行！
        """
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(200, 160)
        chat_1 = Image.new("RGB", (500, 400), (30, 30, 30))
        thumb = src.resize((160, 128), Image.Resampling.BILINEAR)
        chat_1.paste(thumb, (300, 200))

        # 在缩略图左侧垂直居中绘制一个静止不动的灰白色转圈圆环 (x=266..284, y=254..272)
        draw = ImageDraw.Draw(chat_1)
        draw.ellipse((266, 254, 284, 272), outline=(210, 210, 210), width=3)
        # chat_2 与 chat_1 100% 相同（模拟弱网画面完全卡死静止）
        chat_2 = chat_1.copy()

        status = verifier.evaluate(chat_2, src, prev_screenshot=chat_1)
        self.assertTrue(status.matched)
        self.assertTrue(status.has_static_spinner)
        self.assertFalse(status.is_upload_complete)

    def test_spinner_motion_blocks_completion(self) -> None:
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(200, 200)
        chat_1 = Image.new("RGB", (500, 400), (30, 30, 30))
        thumb = src.resize((160, 160), Image.Resampling.BILINEAR)
        chat_1.paste(thumb, (300, 180))

        chat_2 = chat_1.copy()
        draw = ImageDraw.Draw(chat_2)
        draw.rectangle((260, 240, 288, 275), fill=(240, 240, 240))

        status = verifier.evaluate(chat_2, src, prev_screenshot=chat_1)
        self.assertTrue(status.matched)
        self.assertFalse(status.is_upload_complete)

    def test_red_failure_badge_detected(self) -> None:
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(200, 160)
        chat = Image.new("RGB", (500, 400), (30, 30, 30))
        thumb = src.resize((160, 128), Image.Resampling.BILINEAR)
        chat.paste(thumb, (300, 200))

        draw = ImageDraw.Draw(chat)
        draw.ellipse((262, 245, 290, 273), fill=(235, 35, 35))

        status = verifier.evaluate(chat, src, prev_screenshot=chat)
        self.assertTrue(status.matched)
        self.assertTrue(status.has_failure_badge)
        self.assertFalse(status.is_upload_complete)

    def test_wrong_image_rejected(self) -> None:
        verifier = VisualBubbleVerifier()
        src = self._create_gradient_source(200, 160)
        wrong = Image.fromarray(255 - np.asarray(src), mode="RGB")

        chat = Image.new("RGB", (500, 400), (30, 30, 30))
        chat.paste(wrong.resize((160, 128)), (300, 200))

        status = verifier.evaluate(chat, src, prev_screenshot=chat)
        self.assertFalse(status.matched)
        self.assertFalse(status.is_upload_complete)


class TestSequentialSendEngineAndRecovery(unittest.TestCase):
    def test_full_demo_queue_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for i in [1, 2, 10]:
                img = Image.new("RGB", (180, 120), (i * 20, 100, 200 - i * 10))
                img.save(tmp_path / f"{i}.jpg", format="JPEG")

            items, skipped = scan_image_folder(tmp_path)
            self.assertEqual(len(skipped), 0)
            self.assertEqual([it.display_name for it in items], ["1", "2", "10"])

            adapter = DemoWeChatAdapter(simulate_fast_ms=5)
            engine = SequentialSendEngine(adapter, jobs_dir=tmp_path / "jobs")
            job = engine.create_job(tmp_path, items, target_chat="文件传输助手")
            ok, msg = engine.run_job(job, verify_timeout_sec=2.0, stable_duration_sec=0.01)
            self.assertTrue(ok, msg)
            self.assertEqual(
                adapter.sent_records,
                [
                    ("IMAGE", "1.jpg"),
                    ("TEXT", "1"),
                    ("IMAGE", "2.jpg"),
                    ("TEXT", "2"),
                    ("IMAGE", "10.jpg"),
                    ("TEXT", "10"),
                ],
            )

    def test_manual_recovery_send_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            img = Image.new("RGB", (160, 120), (80, 120, 160))
            img.save(tmp_path / "01_风景.png", format="PNG")

            items, _ = scan_image_folder(tmp_path)
            adapter = DemoWeChatAdapter(simulate_fast_ms=5)
            engine = SequentialSendEngine(adapter, jobs_dir=tmp_path / "jobs")
            job = engine.create_job(tmp_path, items, target_chat="文件传输助手")

            job.items[0].stage = ItemStage.NEEDS_MANUAL_VERIFY.value
            engine.save_job(job)

            ok_before, _ = engine.run_job(job)
            self.assertFalse(ok_before)

            engine.apply_manual_decision(job, 0, ManualDecision.SEND_NAME_ONLY)
            ok_after, _ = engine.run_job(job, verify_timeout_sec=2.0, stable_duration_sec=0.01)
            self.assertTrue(ok_after)
            self.assertEqual(adapter.sent_records, [("TEXT", "01_风景")])


class TestZeroPatchAccessibility(unittest.TestCase):
    def test_activate_and_restore(self) -> None:
        act = ZeroPatchAccessibilityActivator()
        state = act.activate(hwnd=None)
        self.assertIsNotNone(state)
        act.restore()


if __name__ == "__main__":
    unittest.main()
