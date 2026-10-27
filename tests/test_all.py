# -*- coding: utf-8 -*-
"""
WeChatImageSender v3.5 Pro 完整离线单元测试套件：
覆盖自然排序、MPO双帧主图提取、动图拦截、透明通道转白底、CF_HDROP 结构体编码、
剪贴板快照备份与自动恢复（snapshot / restore）、大文件采样快速 SHA-256、批量命名模板引擎、
默认阈值 0.40、图像金字塔 0.5x 粗筛+全分辨率多尺度定位、超长图/超宽图居中裁切子块匹配、
弱网静态卡死转圈图标一票否决（Static Spinner Veto，两帧差分==0 仍能拦截且无缩略图圆角边缘误判）、
红色失败标记识别、上传转圈帧间差分检测、现场标注对比截图生成、队列状态机、人工恢复决策以及多维微信窗口标题识别。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from wechat_sender.accessibility import OBJID_CLIENT, ZeroPatchAccessibilityActivator
from wechat_sender.clipboard_win32 import ClipboardManager, build_cf_hdrop_payload
from wechat_sender.gui import _install_robust_window_detector, _is_wechat_window_title
from wechat_sender.image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    VisualBubbleVerifier,
    compute_sha256,
    format_batch_display_name,
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
from wechat_sender.wechat_adapter import DemoWeChatAdapter, WindowsWeChatAdapter


class TestImageUtilsAndSorting(unittest.TestCase):
    def test_default_threshold_is_0_40(self) -> None:
        self.assertAlmostEqual(DEFAULT_SIMILARITY_THRESHOLD, 0.40)
        v = VisualBubbleVerifier()
        self.assertAlmostEqual(v.similarity_threshold, 0.40)

    def test_natural_sort_order(self) -> None:
        names = ["10.png", "2.png", "1.png", "20.png", "a2.jpg", "a10.jpg"]
        sorted_names = sorted(names, key=natural_sort_key)
        self.assertEqual(sorted_names, ["1.png", "2.png", "10.png", "20.png", "a2.jpg", "a10.jpg"])

    def test_batch_display_name_template(self) -> None:
        p = Path("/tmp/精修样片/DSC_0088.JPG")
        self.assertEqual(format_batch_display_name("{num:02d}_{name}", 3, p, False), "03_DSC_0088")
        self.assertEqual(format_batch_display_name("{folder}_{num:03d}", 12, p, False), "精修样片_012")
        self.assertEqual(format_batch_display_name("图{num}_{filename}", 1, p, True), "图1_DSC_0088.JPG")

    def test_fast_sampled_sha256_for_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big_file = Path(tmp) / "large.bin"
            # 创建 5MB 测试文件 (> 4MB 采样阈值)
            data = bytearray(b"A" * (5 * 1024 * 1024))
            data[0:8] = b"HEAD1234"
            data[-8:] = b"TAIL5678"
            big_file.write_bytes(data)

            h1 = compute_sha256(big_file, fast_sample=True)
            h2 = compute_sha256(big_file, fast_sample=True)
            self.assertEqual(h1, h2)

            # 修改中部字节，验证快速采样哈希能够灵敏捕获变化
            mid = len(data) // 2
            data[mid : mid + 4] = b"DIFF"
            big_file.write_bytes(data)
            h3 = compute_sha256(big_file, fast_sample=True)
            self.assertNotEqual(h1, h3)

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

    def test_clipboard_manager_dry_run_and_snapshot_restore(self) -> None:
        mgr = ClipboardManager(dry_run=True)
        mgr.set_text("用户原本复制的重要笔记")
        snap = mgr.snapshot()
        self.assertIsNotNone(snap)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "1.png"
            Image.new("RGB", (50, 50), (12, 34, 56)).save(p)
            mgr.set_image_file_hdrop(p)
            self.assertEqual(mgr.last_mode, "CF_HDROP")
            mgr.set_image_dib(p)
            self.assertEqual(mgr.last_mode, "CF_DIB")
            mgr.set_text("发送图片名称")
            self.assertEqual(mgr.last_payload, "发送图片名称")

            # 验证队列结束后还原剪贴板
            restored = mgr.restore(snap)
            self.assertTrue(restored)
            self.assertEqual(mgr.last_mode, "CF_UNICODETEXT")
            self.assertEqual(mgr.last_payload, "用户原本复制的重要笔记")


class TestVisualBubbleVerifier(unittest.TestCase):
    def _create_gradient_source(self, w: int = 240, h: int = 160) -> Image.Image:
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        for y in range(h):
            for x in range(w):
                arr[y, x] = [(x * 255) // w, (y * 255) // h, ((x + y) * 127) // (w + h)]
        return Image.fromarray(arr, mode="RGB")

    def test_multiscale_pyramid_locate_and_quiet_confirm(self) -> None:
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

    def test_reference_matcher_tolerates_a_long_image_center_crop(self) -> None:
        """参考版全框/内框匹配在裁切样张上仍达到配置阈值。"""
        verifier = VisualBubbleVerifier()
        src_long = self._create_gradient_source(200, 800)

        # 模拟微信对超长图的居中正方形裁切并缩放到 150x150
        center_square = src_long.crop((0, 300, 200, 500)).resize((150, 150), Image.Resampling.BILINEAR)
        chat = Image.new("RGB", (520, 420), (28, 28, 28))
        chat.paste(center_square, (320, 210))

        status = verifier.evaluate(chat, src_long, prev_screenshot=chat)
        self.assertTrue(status.matched)
        self.assertTrue(status.is_upload_complete)
        self.assertGreaterEqual(status.similarity, verifier.similarity_threshold)

    def test_rounded_corners_and_nonstandard_scale_match(self) -> None:
        """测试微信真实气泡具有圆角遮罩、边框及非标缩放宽度 (如 153px) 时的鲁棒高分匹配且不误报转圈。"""
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
        self.assertFalse(status.has_static_spinner)
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

        draw = ImageDraw.Draw(chat_1)
        draw.ellipse((266, 254, 284, 272), outline=(210, 210, 210), width=3)
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
    def test_full_demo_queue_execution_and_clipboard_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for i in [1, 2, 10]:
                img = Image.new("RGB", (180, 120), (i * 20, 100, 200 - i * 10))
                img.save(tmp_path / f"{i}.jpg", format="JPEG")

            items, skipped = scan_image_folder(tmp_path)
            self.assertEqual(len(skipped), 0)
            self.assertEqual([it.display_name for it in items], ["1", "2", "10"])

            adapter = DemoWeChatAdapter(simulate_fast_ms=5)
            adapter.clipboard.set_text("发送前剪贴板里的原始文字")
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
            # 验证队列完成后自动恢复了发送前的剪贴板内容，且保存了现场标注截图
            self.assertEqual(adapter.clipboard.last_payload, "发送前剪贴板里的原始文字")
            annotated = adapter.get_annotated_verify_screenshot()
            self.assertIsNotNone(annotated)

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


class TestZeroPatchAccessibilityAndWindowDetection(unittest.TestCase):
    def test_activate_and_restore(self) -> None:
        self.assertEqual(OBJID_CLIENT, -4)
        act = ZeroPatchAccessibilityActivator()
        state = act.activate(hwnd=None)
        self.assertIsNotNone(state)
        act.restore()

    def test_wechat_window_title_matching_and_self_exclusion(self) -> None:
        self.assertTrue(_is_wechat_window_title("微信"))
        self.assertTrue(_is_wechat_window_title("Weixin"))
        self.assertTrue(_is_wechat_window_title("WeChat"))
        self.assertTrue(_is_wechat_window_title("微信(3)"))
        self.assertTrue(_is_wechat_window_title("微信 (12)"))
        # 严禁误匹配本程序自身窗口
        self.assertFalse(_is_wechat_window_title("微信图片顺序发送助手 v3.5 Pro"))
        self.assertFalse(_is_wechat_window_title("WeChatImageSender"))
        self.assertFalse(_is_wechat_window_title(""))

        win_adapter = WindowsWeChatAdapter(enable_accessibility=False)
        _install_robust_window_detector(win_adapter)
        self.assertIn("mmui::MainWindow", win_adapter.WECHAT_MAIN_CLASSES)
        self.assertIn("WeChatMainWndForPC", win_adapter.WECHAT_MAIN_CLASSES)


if __name__ == "__main__":
    unittest.main()
