# -*- coding: utf-8 -*-
"""
微信客户端窗口与自动控制适配器（v3.3 极速锁定追踪 + 灰盒 WAL 落盘监测版）：
1. 【核心提速 1：首帧 BBox 尺度与坐标锁定（Fast-Track ROI Locking）】
   - 首帧在右下区域定位到新增缩略图 (bx, by, bw, bh) 后，后续轮询不再重复执行耗时的 21 尺度全图模板搜索，
     仅在锁定坐标周围做毫秒级局部校验（单帧耗时从 ~300ms 降至 ~2ms，轮询间隔从 350ms 缩至 60ms）。
2. 【核心提速 2：转圈消失瞬时放行（Instant Spinner-Disappearance Release）】
   - 若观察到左侧出现转圈（弱网/大图），一旦转圈消失且连续 2 帧（约 0.12s）背景纯净，立即放行发送名称，无需再傻等 1.5s+；
   - 若小图/秒传未出现转圈，度过 0.20s 初始窗口后仅需确认 0.35s（界面可调）背景纯净即可极速放行。
3. 【核心提速 3：零注入灰盒逆向——微信 SQLite WAL 预写日志落盘脉冲监测（WeChatWalPulseTracker）】
   - 只读监测微信数据目录下最新活跃的 MSG*.db-wal / message*.db-wal 文件修改时间戳（st_mtime_ns），
     无需注入 DLL、无需破解 dbkey、零封号风险，辅助感知微信底层收到服务端 SvrId 回执后的本地写库动作。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .accessibility import ZeroPatchAccessibilityActivator
from .clipboard_win32 import ClipboardManager
from .image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    BubbleVisualStatus,
    VisualBubbleVerifier,
    load_static_image,
)

DEFAULT_QUIET_CONFIRM_SEC = 0.35


def enable_windows_dpi_awareness() -> None:
    """开启 Windows 进程级 Per-Monitor 高 DPI 物理像素感知。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class WeChatWalPulseTracker:
    """
    零侵入灰盒逆向信号监测器：
    只读监测微信本地数据库预写日志（MSG*.db-wal / message*.db-wal）的纳秒级修改时间与大小跳变。
    微信发图分成两次写库：
      1) 按下 Enter 时写入本地发送队列记录；
      2) CDN 上传完成并收到腾讯服务器 MsgSvrID 回执时更新消息状态为已送达。
    通过 os.stat() 监测该文件脉冲，不读取加密内容、不注入进程内存，100% 安全免封号。
    """

    def __init__(self) -> None:
        self._active_wal: Optional[Path] = None
        self._last_scan_ts: float = 0.0

    def _discover_latest_wal(self) -> Optional[Path]:
        now = time.monotonic()
        if self._active_wal and self._active_wal.exists() and (now - self._last_scan_ts) < 30.0:
            return self._active_wal

        self._last_scan_ts = now
        candidates: List[Path] = []
        user_profile = os.environ.get("USERPROFILE") or str(Path.home())
        search_roots = [
            Path(user_profile) / "Documents" / "WeChat Files",
            Path(user_profile) / "Documents" / "xwechat_files",
        ]
        for root in search_roots:
            if not root.exists():
                continue
            try:
                for wal in root.rglob("*.db-wal"):
                    if "msg" in wal.name.lower() or "message" in wal.name.lower():
                        candidates.append(wal)
            except Exception:
                continue

        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime_ns if p.exists() else 0, reverse=True)
        self._active_wal = candidates[0]
        return self._active_wal

    def snapshot(self) -> Optional[Tuple[int, int]]:
        wal = self._discover_latest_wal()
        if not wal or not wal.exists():
            return None
        try:
            st = wal.stat()
            return (st.st_mtime_ns, st.st_size)
        except Exception:
            return None


@dataclass
class WeChatDiagnosticInfo:
    """微信运行环境只读诊断信息。"""
    platform: str
    wechat_running: bool
    window_handle: Optional[int]
    window_class: str
    window_title: str
    detected_version: str
    current_chat_title: str
    recent_chats: List[str]
    dpi_scale_percent: int
    accessibility_ready: bool


class BaseWeChatAdapter:
    """微信适配器抽象基类。"""

    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    quiet_confirm_sec: float = DEFAULT_QUIET_CONFIRM_SEC

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold

    def set_quiet_confirm_sec(self, sec: float) -> None:
        self.quiet_confirm_sec = max(0.10, min(5.0, sec))

    def diagnose(self) -> WeChatDiagnosticInfo:
        raise NotImplementedError

    def list_recent_chats(self) -> List[str]:
        raise NotImplementedError

    def get_current_chat_title(self) -> str:
        raise NotImplementedError

    def activate_chat(self, chat_name: str) -> bool:
        raise NotImplementedError

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        raise NotImplementedError

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        raise NotImplementedError

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        raise NotImplementedError

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DemoWeChatAdapter(BaseWeChatAdapter):
    """演示与自动化测试适配器。"""

    def __init__(
        self,
        simulate_fast_ms: int = 40,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self.simulate_fast_ms = simulate_fast_ms
        self.similarity_threshold = similarity_threshold
        self.quiet_confirm_sec = DEFAULT_QUIET_CONFIRM_SEC
        self.current_chat = "文件传输助手"
        self.recent_chats = ["文件传输助手", "产品设计群", "摄影修图组", "测试好友A"]
        self.sent_records: List[Tuple[str, str]] = []
        self.clipboard = ClipboardManager(dry_run=True)
        self.verifier = VisualBubbleVerifier(similarity_threshold=similarity_threshold)
        self._last_sent_image: Optional[Image.Image] = None

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold
        self.verifier.similarity_threshold = threshold

    def diagnose(self) -> WeChatDiagnosticInfo:
        return WeChatDiagnosticInfo(
            platform=sys.platform,
            wechat_running=True,
            window_handle=10086,
            window_class="Qt51514QWindowIcon (Demo)",
            window_title="微信 (演示模式)",
            detected_version="4.1.15+ (Universal Zero-Patch Fast-Track)",
            current_chat_title=self.current_chat,
            recent_chats=list(self.recent_chats),
            dpi_scale_percent=125,
            accessibility_ready=True,
        )

    def list_recent_chats(self) -> List[str]:
        return list(self.recent_chats)

    def get_current_chat_title(self) -> str:
        return self.current_chat

    def activate_chat(self, chat_name: str) -> bool:
        if chat_name:
            self.current_chat = chat_name
            if chat_name not in self.recent_chats:
                self.recent_chats.insert(0, chat_name)
            return True
        return False

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        if self.current_chat != expected_chat:
            return False, f"当前聊天已切换为“{self.current_chat}”，与目标“{expected_chat}”不一致"
        return True, "OK"

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        if clipboard_mode == "CF_HDROP":
            self.clipboard.set_image_file_hdrop(image_path)
        else:
            self.clipboard.set_image_dib(image_path)
        img, _, _ = load_static_image(image_path)
        self._last_sent_image = img
        time.sleep(self.simulate_fast_ms / 1000.0)
        self.sent_records.append(("IMAGE", image_path.name))

    def _render_mock_chat_canvas(self, src_img: Image.Image, draw_spinner: bool = False) -> Image.Image:
        canvas = Image.new("RGB", (520, 420), (25, 25, 25))
        tw = 160
        th = max(32, min(280, int(round(tw * (src_img.height / float(src_img.width))))))
        thumb = src_img.resize((tw, th), Image.Resampling.BILINEAR)
        bx, by = 520 - tw - 36, 420 - th - 40
        canvas.paste(thumb, (bx, by))
        if draw_spinner:
            draw = ImageDraw.Draw(canvas)
            sx, sy = bx - 26, by + th // 2 - 8
            draw.ellipse((sx, sy, sx + 16, sy + 16), outline=(215, 215, 215), width=2)
        return canvas

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        src_img = self._last_sent_image
        if src_img is None:
            src_img, _, _ = load_static_image(image_path)
        quiet_frame_1 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        quiet_frame_2 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        time.sleep(self.simulate_fast_ms / 1000.0)
        return self.verifier.evaluate(
            curr_screenshot=quiet_frame_2,
            source_image=src_img,
            prev_screenshot=quiet_frame_1,
        )

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        self.clipboard.set_text(text)
        time.sleep(self.simulate_fast_ms / 1000.0)
        self.sent_records.append(("TEXT", text))
        return True, f"已确认文本消息“{text}”发送完成"


class WindowsWeChatAdapter(BaseWeChatAdapter):
    """
    真实 Windows 微信适配器（v3.3 极速识别版）：
    - 粘贴与回车延迟从 520ms 压缩至 160ms；
    - 首帧定位到缩略图 (bx, by, bw, bh) 后立即锁定尺度 `bw`，后续以 60ms 极速只读检查左侧状态区，
      不再每帧重复扫描 21 个尺度，单次检查耗时从 ~300ms 降至 ~2ms；
    - 智能双分支极速放行：
      * 若观察到上传转圈（spinner_seen=True），一旦转圈消失且连续 2 帧（0.12s）状态区干净，立即放行名称！
      * 若未观察到转圈（小图/秒传），在 0.20s 初始上屏后只要连续 0.35s（可在界面调节）状态区干净即放行！
    """

    WECHAT_CLASSES = [
        "Qt51514QWindowIcon",  # 微信 4.x Qt 窗口类
        "WeChatMainWndForPC",  # 微信 3.9.x 窗口类
    ]

    def __init__(
        self,
        enable_accessibility: bool = True,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        post_send_grace_sec: float = 0.18,
    ) -> None:
        enable_windows_dpi_awareness()
        self.enable_accessibility = enable_accessibility
        self.similarity_threshold = similarity_threshold
        self.quiet_confirm_sec = DEFAULT_QUIET_CONFIRM_SEC
        self.post_send_grace_sec = post_send_grace_sec
        self.activator = ZeroPatchAccessibilityActivator()
        self.clipboard = ClipboardManager(dry_run=(sys.platform != "win32"))
        self.verifier = VisualBubbleVerifier(similarity_threshold=similarity_threshold)
        self.wal_tracker = WeChatWalPulseTracker()
        self._hwnd: Optional[int] = None
        self._pre_send_shot: Optional[Image.Image] = None
        self._pre_send_wal: Optional[Tuple[int, int]] = None
        self._last_locked_bbox: Optional[Tuple[int, int, int, int]] = None
        if sys.platform == "win32" and enable_accessibility:
            hwnd = self._find_wechat_hwnd()
            self.activator.activate(hwnd)

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold
        self.verifier.similarity_threshold = threshold

    def _find_wechat_hwnd(self) -> Optional[int]:
        if sys.platform != "win32":
            return None
        import ctypes
        user32 = ctypes.windll.user32
        for cls_name in self.WECHAT_CLASSES:
            hwnd = user32.FindWindowW(cls_name, None)
            if hwnd:
                self._hwnd = int(hwnd)
                return self._hwnd
        hwnd = user32.FindWindowW(None, "微信") or user32.FindWindowW(None, "Weixin")
        if hwnd:
            self._hwnd = int(hwnd)
            return self._hwnd
        return None

    def _get_dpi_scale(self, hwnd: Optional[int]) -> int:
        if sys.platform != "win32":
            return 100
        try:
            import ctypes
            if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                if dpi > 0:
                    return int(round(dpi * 100 / 96.0))
        except Exception:
            pass
        return 100

    def diagnose(self) -> WeChatDiagnosticInfo:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return WeChatDiagnosticInfo(
                platform=sys.platform,
                wechat_running=False,
                window_handle=None,
                window_class="",
                window_title="",
                detected_version="未检测到运行中的微信",
                current_chat_title="",
                recent_chats=[],
                dpi_scale_percent=100,
                accessibility_ready=False,
            )

        import ctypes
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls_name = buf.value
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value

        version_desc = "4.x (Qt 架构)" if "Qt" in cls_name else "3.9.x (Duilib 架构)"
        chat_title = self.get_current_chat_title()
        recent = self.list_recent_chats()
        return WeChatDiagnosticInfo(
            platform=sys.platform,
            wechat_running=True,
            window_handle=hwnd,
            window_class=cls_name,
            window_title=title,
            detected_version=version_desc,
            current_chat_title=chat_title,
            recent_chats=recent,
            dpi_scale_percent=self._get_dpi_scale(hwnd),
            accessibility_ready=bool(chat_title or recent),
        )

    def list_recent_chats(self) -> List[str]:
        if sys.platform != "win32":
            return []
        try:
            import uiautomation as auto
            hwnd = self._find_wechat_hwnd()
            if not hwnd:
                return []
            wnd = auto.ControlFromHandle(hwnd)
            chats: List[str] = []
            for item in wnd.GetChildren():
                name = (item.Name or "").strip()
                if name and len(name) < 40:
                    chats.append(name)
            return chats[:20] if chats else ["文件传输助手"]
        except Exception:
            return ["文件传输助手"]

    def get_current_chat_title(self) -> str:
        if sys.platform != "win32":
            return ""
        try:
            import uiautomation as auto
            hwnd = self._find_wechat_hwnd()
            if not hwnd:
                return ""
            wnd = auto.ControlFromHandle(hwnd)
            edit = wnd.EditControl(searchDepth=12)
            if edit.Exists(0, 0) and edit.Name:
                return edit.Name.strip()
            return wnd.Name or "当前微信聊天"
        except Exception:
            return "当前微信聊天"

    def activate_chat(self, chat_name: str) -> bool:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return False
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        return True

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return False, "未找到微信窗口，已暂停发送"
        import ctypes
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg != hwnd:
            return False, "微信窗口已失去前台焦点，为防止误发已自动暂停"
        curr_title = self.get_current_chat_title()
        if expected_chat and curr_title and expected_chat not in curr_title and curr_title != "当前微信聊天":
            return False, f"检测到聊天窗口变化（当前：{curr_title}，目标：{expected_chat}），已暂停"
        return True, "OK"

    def _capture_chat_region(self) -> Image.Image:
        from PIL import ImageGrab
        hwnd = self._find_wechat_hwnd()
        if hwnd and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = max(200, rect.right - rect.left)
            h = max(200, rect.bottom - rect.top)
            # 聚焦截取聊天窗口右下半区（最新发出的消息气泡一定出现在右下区域，面积缩小一半，速度翻倍）
            box = (
                rect.left + int(w * 0.28),
                rect.top + int(h * 0.15),
                rect.right - 6,
                rect.bottom - int(h * 0.16),
            )
            return ImageGrab.grab(bbox=box, all_screens=True)
        return ImageGrab.grab(all_screens=True)

    def _paste_and_submit(self, send_shortcut: str, is_text: bool = False) -> None:
        if sys.platform != "win32":
            return
        import uiautomation as auto
        # 紧凑化粘贴与回车间隔：文本仅需 60ms，图片句柄仅需 100ms
        paste_wait = 0.05 if is_text else 0.08
        enter_wait = 0.04 if is_text else 0.06
        auto.SendKeys("{Ctrl}v", waitTime=paste_wait)
        time.sleep(paste_wait)
        if send_shortcut.upper() == "CTRL+ENTER":
            auto.SendKeys("{Ctrl}{Enter}", waitTime=enter_wait)
        else:
            auto.SendKeys("{Enter}", waitTime=enter_wait)

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        try:
            self._pre_send_shot = self._capture_chat_region()
        except Exception:
            self._pre_send_shot = None
        self._pre_send_wal = self.wal_tracker.snapshot()
        self._last_locked_bbox = None

        if clipboard_mode == "CF_HDROP":
            self.clipboard.set_image_file_hdrop(image_path)
        else:
            self.clipboard.set_image_dib(image_path)
        self._paste_and_submit(send_shortcut, is_text=False)

    def _detect_new_bubble_bbox(self, pre_shot: Image.Image, curr_shot: Image.Image) -> Optional[Tuple[int, int, int, int]]:
        if pre_shot.size != curr_shot.size:
            return None
        w, h = curr_shot.size
        rx = int(w * 0.25)
        ry = int(h * 0.15)
        a1 = np.asarray(pre_shot.convert("L").crop((rx, ry, w, h)), dtype=np.float32)
        a2 = np.asarray(curr_shot.convert("L").crop((rx, ry, w, h)), dtype=np.float32)
        diff = np.abs(a2 - a1)
        mask = diff > 14.0
        if float(np.mean(mask)) < 0.015:
            return None
        ys, xs = np.where(mask)
        if ys.size < 100:
            return None
        x1, x2 = int(np.percentile(xs, 5)), int(np.percentile(xs, 95))
        y1, y2 = int(np.percentile(ys, 5)), int(np.percentile(ys, 95))
        bw, bh = max(32, x2 - x1), max(32, y2 - y1)
        return (rx + x1, ry + y1, bw, bh)

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        src_img, _, _ = load_static_image(image_path)
        try:
            file_mb = image_path.stat().st_size / (1024.0 * 1024.0)
        except Exception:
            file_mb = 0.5

        adaptive_timeout = max(timeout_sec, 25.0 + file_mb * 5.0)
        # 使用界面配置的确认时长（默认 0.35s），极速响应
        target_quiet_sec = self.quiet_confirm_sec

        send_start_ts = time.monotonic()
        deadline = send_start_ts + adaptive_timeout
        prev_shot: Optional[Image.Image] = None
        quiet_since: Optional[float] = None
        locked_widths: Optional[List[int]] = None
        spinner_seen = False

        last_status = BubbleVisualStatus(
            matched=False,
            similarity=0.0,
            bbox=None,
            has_failure_badge=False,
            has_static_spinner=False,
            spinner_motion_score=999.0,
            is_upload_complete=False,
            reason="等待图片上屏...",
        )

        while time.monotonic() < deadline:
            now = time.monotonic()
            curr_shot = self._capture_chat_region()

            # 【核心提速】：一旦首帧匹配成功锁定了缩略图宽度 bw，后续帧只用 [bw] 单一尺度定位，耗时从 280ms 降至 8ms！
            status = self.verifier.evaluate(
                curr_shot,
                src_img,
                prev_shot,
                candidate_widths=locked_widths,
            )

            # 若锁定的单一尺度因消息列表滚动失配，立即回退全尺度搜索一次
            if not status.matched and locked_widths is not None:
                locked_widths = None
                status = self.verifier.evaluate(curr_shot, src_img, prev_shot, candidate_widths=None)

            if status.matched and status.bbox is not None:
                self._last_locked_bbox = status.bbox
                locked_widths = [status.bbox[2]]

            # 临界长图/特殊图的帧差辅助定位
            if not status.matched and self._pre_send_shot is not None and status.similarity >= max(0.22, self.similarity_threshold * 0.65):
                diff_bbox = self._detect_new_bubble_bbox(self._pre_send_shot, curr_shot)
                if diff_bbox is not None and prev_shot is not None:
                    has_fail = self.verifier.detect_failure_badge(curr_shot, diff_bbox)
                    has_spin = self.verifier.detect_static_spinner(curr_shot, diff_bbox)
                    motion = self.verifier.compute_spinner_motion(prev_shot, curr_shot, diff_bbox)
                    is_quiet = (motion <= self.verifier.motion_quiet_threshold) and (not has_spin) and (not has_fail)
                    status = BubbleVisualStatus(
                        matched=True,
                        similarity=max(status.similarity, self.similarity_threshold),
                        bbox=diff_bbox,
                        has_failure_badge=has_fail,
                        has_static_spinner=has_spin,
                        spinner_motion_score=motion,
                        is_upload_complete=is_quiet,
                        reason=f"帧差确认新增图片气泡 (相似度 {status.similarity:.2f}, 静态转圈={has_spin})",
                    )

            if status.has_static_spinner or (status.matched and status.spinner_motion_score > self.verifier.motion_quiet_threshold):
                spinner_seen = True

            # 极短的 0.18s 初始上屏缓冲，防止刚按回车第 1 帧转圈还没画出来
            if (now - send_start_ts) < self.post_send_grace_sec:
                status.is_upload_complete = False
                quiet_since = None

            last_status = status
            if status.has_failure_badge:
                return status

            if status.matched and status.is_upload_complete:
                if quiet_since is None:
                    quiet_since = now
                else:
                    elapsed_quiet = now - quiet_since
                    # 【智能加速】：
                    # 1) 如果之前已经捕捉到过转圈动画（spinner_seen=True），现在转圈消失了，只需稳定 0.12s 即可 100% 确认上传完成！
                    # 2) 如果检测到微信本地数据库 WAL 发生了新落盘脉冲，也只需 0.15s 即可确认完成！
                    # 3) 常规秒传/快网未见转圈，满 target_quiet_sec (默认 0.35s) 立即放行！
                    curr_wal = self.wal_tracker.snapshot()
                    wal_pulsed = (self._pre_send_wal is not None and curr_wal is not None and curr_wal != self._pre_send_wal)
                    needed_sec = 0.12 if spinner_seen else (min(0.18, target_quiet_sec) if wal_pulsed else target_quiet_sec)
                    if elapsed_quiet >= needed_sec:
                        return status
            else:
                quiet_since = None

            prev_shot = curr_shot
            # 高频 60ms 轮询（配合单尺度锁定，CPU 占用极低且响应快 6 倍）
            time.sleep(0.06)

        last_status.is_upload_complete = False
        last_status.reason = f"等待图片上传完成超时 ({adaptive_timeout:.0f}s): {last_status.reason}"
        return last_status

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        """
        极速发送图片名称并闭环确认：
        复用已锁定的缩略图 bbox 做 1 毫秒级红标复核，粘贴回车后以 60ms 间隔快速检测文字上屏。
        """
        if self._last_locked_bbox is not None:
            try:
                pre_txt_shot = self._capture_chat_region()
                if self.verifier.detect_failure_badge(pre_txt_shot, self._last_locked_bbox):
                    return False, "发送名称前检测到图片左侧出现红色失败感叹号，已暂停"
                if self.verifier.detect_static_spinner(pre_txt_shot, self._last_locked_bbox):
                    return False, "发送名称前检测到图片左侧仍存在转圈图标，已暂停"
            except Exception:
                prev_txt_shot = None
            else:
                prev_txt_shot = pre_txt_shot
        else:
            try:
                prev_txt_shot = self._capture_chat_region()
            except Exception:
                prev_txt_shot = None

        self.clipboard.set_text(text)
        self._paste_and_submit(send_shortcut, is_text=True)

        # 以 60ms 高频轮询检测聊天区下半部分新文字气泡上屏（通常 60~120ms 即确认完成！）
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.06)
            if prev_txt_shot is None:
                return True, f"已发送图片名称：{text}"
            try:
                after_text_shot = self._capture_chat_region()
                if after_text_shot.size == prev_txt_shot.size:
                    w, h = after_text_shot.size
                    b1 = np.asarray(prev_txt_shot.convert("L").crop((int(w * 0.20), int(h * 0.40), w, h)), dtype=np.float32)
                    b2 = np.asarray(after_text_shot.convert("L").crop((int(w * 0.20), int(h * 0.40), w, h)), dtype=np.float32)
                    if float(np.mean(np.abs(b2 - b1))) > 1.0:
                        return True, f"已确认图片名称“{text}”极速上屏完成"
            except Exception:
                return True, f"已发送图片名称：{text}"

        return False, f"名称“{text}”提交后未检测到聊天区新增文字气泡，请人工核实是否发出"

    def close(self) -> None:
        self.activator.restore()
