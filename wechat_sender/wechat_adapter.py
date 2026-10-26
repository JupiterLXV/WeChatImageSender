# -*- coding: utf-8 -*-
"""
微信客户端窗口与自动控制适配器：
1. 【多维窗口枚举识别】结合进程名、窗口类名和尺寸查找微信窗口，具体兼容性需实测；
2. 【PrintWindow 后台抗遮挡抓图 + ImageGrab 双引擎】支持在通知弹窗遮挡时直接从窗口缓冲区抓取画面；
3. 【手动定位】用户先打开目标聊天，再由程序刷新当前微信窗口；保留原版可选搜索方式；
4. 【现场视觉对比快照】保存最新校验截图与定位框（get_annotated_verify_screenshot），供待核实弹窗直观并排展示；
5. 【原版发送算法】保留图像金字塔、定位框锁定及原名称提交逻辑，不以 OCR 读出名称作为前提。
"""

import contextlib
import ctypes
import math
import os
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .accessibility import ZeroPatchAccessibilityActivator
from .clipboard_win32 import ClipboardManager
from .capture_win32 import capture_chat_frame
from .windows_ocr import normalized_text, recognize as recognize_local_text
from .image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    BubbleVisualStatus,
    VisualBubbleVerifier,
    load_static_image,
)

DEFAULT_QUIET_CONFIRM_SEC = 1.5

GENERIC_CHAT_TITLES = {
    "",
    "微信",
    "Weixin",
    "WeChat",
    "当前微信聊天",
    "搜索",
    "Search",
    "输入",
    "编辑",
    "发送",
    "发送(S)",
    "聊天信息",
}


def enable_windows_dpi_awareness() -> None:
    """开启 Windows 进程级 Per-Monitor 高 DPI 物理像素感知。"""
    if sys.platform != "win32":
        return
    try:
        try:
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        res = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if res != 0:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


@contextlib.contextmanager
def _uia_thread_scope() -> Iterator[None]:
    """确保在后台线程调用 uiautomation 时正确初始化当前线程的 Windows COM 环境。"""
    if sys.platform != "win32":
        yield
        return
    initializer = None
    try:
        import uiautomation as auto
        initializer = getattr(auto, "UIAutomationInitializerInThread", None)
    except Exception:
        pass
    if initializer is None:
        yield
    else:
        # 只处理初始化查找；调用方异常必须原样传播，不能捕获 yield 后再次 yield。
        with initializer():
            yield


class WeChatWalPulseTracker:
    """
    零侵入灰盒逆向信号监测器：
    只读监测微信本地数据库预写日志（MSG*.db-wal / message*.db-wal）的纳秒级修改时间与大小跳变。
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
    auto_search_chat: bool = False
    prefer_print_window: bool = False

    def __init__(self) -> None:
        self.last_verify_screenshot: Optional[Image.Image] = None
        self.last_verify_bbox: Optional[Tuple[int, int, int, int]] = None
        self.last_verify_status: Optional[BubbleVisualStatus] = None
        self.clipboard: ClipboardManager = ClipboardManager(dry_run=True)
        self.stop_callback: Optional[Callable[[], bool]] = None

    def set_stop_callback(self, callback: Optional[Callable[[], bool]]) -> None:
        """让正在等待发送确认的适配器及时响应队列暂停。"""
        self.stop_callback = callback

    def _should_stop(self) -> bool:
        return self.stop_callback is not None and bool(self.stop_callback())

    def _wait_or_stop(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    @staticmethod
    def _unconfirmed_status(reason: str) -> BubbleVisualStatus:
        return BubbleVisualStatus(False, 0.0, None, False, False, 999.0, False, reason)

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold

    def set_quiet_confirm_sec(self, sec: float) -> None:
        self.quiet_confirm_sec = max(1.5, min(5.0, sec))

    def image_status_allows_name(self, status: BubbleVisualStatus) -> bool:
        threshold = getattr(getattr(self, "verifier", None), "motion_quiet_threshold", 1.8)
        return (status.matched and status.bbox is not None and status.is_upload_complete
                and not status.has_failure_badge and not status.has_static_spinner
                and math.isfinite(status.spinner_motion_score)
                and status.spinner_motion_score <= threshold)

    def confirm_image_before_name(self, image_path: Path) -> Tuple[bool, str]:
        """演示适配器无需桌面复查；真实适配器必须覆写。"""
        return True, "OK"

    def set_auto_search_chat(self, enabled: bool) -> None:
        self.auto_search_chat = bool(enabled)

    def set_prefer_print_window(self, enabled: bool) -> None:
        self.prefer_print_window = bool(enabled)

    def get_annotated_verify_screenshot(self) -> Optional[Image.Image]:
        """
        返回带有识别框标注的最近一次微信聊天区域截图（用于在“待核实项”弹窗中与原图并排直观对比）：
        - 蓝色实线框：定位到的图片气泡区域 (x, y, w, h)；
        - 琥珀色虚线框：左侧转圈/红标状态检测区域 (x - 42 .. x - 12)。
        """
        if self.last_verify_screenshot is None:
            return None
        canvas = self.last_verify_screenshot.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        if self.last_verify_bbox is not None:
            bx, by, bw, bh = self.last_verify_bbox
            # 绘制缩略图定位框
            draw.rectangle((bx, by, bx + bw, by + bh), outline=(26, 115, 232), width=3)
            # 绘制左侧状态检测区
            for _side, region in VisualBubbleVerifier._status_regions(canvas, self.last_verify_bbox):
                draw.rectangle(region, outline=(245, 175, 45), width=2)
        return canvas

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
        super().__init__()
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
            detected_version="4.1.15+ (Universal Zero-Patch Pyramid Engine)",
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
            if self.auto_search_chat:
                self.activate_chat(expected_chat)
                return True, "OK"
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
        aspect = src_img.height / float(max(1, src_img.width))
        if aspect > 2.4:
            # 模拟微信对超长图的居中裁切显示
            crop_h = min(src_img.height, src_img.width)
            cy = (src_img.height - crop_h) // 2
            cropped = src_img.crop((0, cy, src_img.width, cy + crop_h))
            thumb = cropped.resize((tw, tw), Image.Resampling.BILINEAR)
            th = tw
        else:
            th = max(32, min(280, int(round(tw * aspect))))
            thumb = src_img.resize((tw, th), Image.Resampling.BILINEAR)
        bx, by = 520 - tw - 36, 420 - th - 40
        canvas.paste(thumb, (bx, by))
        if draw_spinner:
            draw = ImageDraw.Draw(canvas)
            sx, sy = bx - 28, by + th // 2 - 8
            draw.ellipse((sx, sy, sx + 16, sy + 16), outline=(215, 215, 215), width=2)
        return canvas

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        if self._should_stop():
            return self._unconfirmed_status("已暂停；图片可能已提交，请人工核实")
        src_img = self._last_sent_image
        if src_img is None:
            src_img, _, _ = load_static_image(image_path)
        quiet_frame_1 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        quiet_frame_2 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        if self._wait_or_stop(self.simulate_fast_ms / 1000.0):
            return self._unconfirmed_status("已暂停；图片可能已提交，请人工核实")
        status = self.verifier.evaluate(
            curr_screenshot=quiet_frame_2,
            source_image=src_img,
            prev_screenshot=quiet_frame_1,
        )
        self.last_verify_screenshot = quiet_frame_2
        self.last_verify_bbox = status.bbox
        self.last_verify_status = status
        return status

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        if self._should_stop():
            return False, "已暂停，名称尚未提交"
        self.clipboard.set_text(text)
        if self._wait_or_stop(self.simulate_fast_ms / 1000.0):
            return False, "已暂停，名称发送状态未确认"
        self.sent_records.append(("TEXT", text))
        return True, f"已确认文本消息“{text}”发送完成"


class WindowsWeChatAdapter(BaseWeChatAdapter):
    """
    真实 Windows 微信适配器（v3.5 Pro 全栈增强版）：
    - 多维窗口枚举（EnumWindows + 进程名 + 窗口类名 + 尺寸评分）；
    - 支持 PrintWindow(PW_RENDERFULLCONTENT) 抗遮挡后台抓图 + ImageGrab 双引擎无缝回退；
    - 支持 Ctrl+F 自动搜索定位微信联系人/群聊；
    - 保存现场截图与 BBox 供待核实弹窗直观对比。
    """

    WECHAT_MAIN_CLASSES = (
        "mmui::MainWindow",      # 微信 4.0.1+ / 4.1+ 主窗口类
        "WeChatMainWndForPC",    # 微信 3.9.x 主窗口类
        "WeixinMainWndForPC",    # 微信 4.x 兼容主窗口类
        "Qt51514QWindowIcon",    # 微信 4.0 Qt 主窗口类
    )

    WECHAT_CHAT_CLASSES = (
        "mmui::ChatSingleWindow",  # 微信 4.x 独立聊天窗口类
        "ChatWnd",                 # 微信 3.9.x 独立聊天窗口类
    )

    WECHAT_CLASSES = list(WECHAT_MAIN_CLASSES) + list(WECHAT_CHAT_CLASSES)

    IGNORED_CLASSES = {
        "WeChatShadowWnd",
        "mmui::ShadowWindow",
        "TrayNotifyWnd",
        "Shell_TrayWnd",
        "Qt51514QWindowToolSaveBits",
        "Qt51514QWindowPopupSaveBits",
        "CefWebViewWnd",
        "WeChatLoginWndForPC",
        "mmui::LoginWindow",
        "IME",
        "MSCTFIME UI",
    }

    WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe", "wechatappex.exe"}

    def __init__(
        self,
        enable_accessibility: bool = True,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        post_send_grace_sec: float = 0.18,
    ) -> None:
        super().__init__()
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

    @staticmethod
    def _get_process_name_by_pid(pid: int) -> str:
        if sys.platform != "win32" or pid <= 0:
            return ""
        try:
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            h_proc = kernel32.OpenProcess(0x1000, False, wintypes.DWORD(pid))
            if not h_proc:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(512)
                size = wintypes.DWORD(512)
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            pass
        return ""

    @classmethod
    def _is_wechat_title(cls, title: str) -> bool:
        t = (title or "").strip()
        if not t or "微信图片顺序发送助手" in t or "WeChatImageSender" in t:
            return False
        if t in ("微信", "Weixin", "WeChat"):
            return True
        for prefix in ("微信(", "微信 (", "微信（", "Weixin(", "Weixin (", "WeChat(", "WeChat ("):
            if t.startswith(prefix):
                return True
        return False

    def _find_wechat_hwnd(self, target_chat: Optional[str] = None) -> Optional[int]:
        if sys.platform != "win32":
            return None
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        current_pid = os.getpid()
        fg_hwnd = int(user32.GetForegroundWindow() or 0)
        target_norm = (target_chat or "").strip()
        if target_norm in GENERIC_CHAT_TITLES:
            target_norm = ""

        candidates: List[Tuple[int, int]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            try:
                h_val = int(hwnd or 0)
                if not h_val:
                    return True

                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(wintypes.HWND(h_val), ctypes.byref(pid))
                if pid.value == current_pid or pid.value == 0:
                    return True

                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(wintypes.HWND(h_val), cls_buf, 256)
                cls_name = cls_buf.value.strip()
                if not cls_name or cls_name in self.IGNORED_CLASSES or cls_name.startswith("Tk"):
                    return True

                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(wintypes.HWND(h_val), title_buf, 256)
                title = title_buf.value.strip()
                if "微信图片顺序发送助手" in title or "WeChatImageSender" in title:
                    return True

                is_visible = bool(user32.IsWindowVisible(wintypes.HWND(h_val)))
                is_iconic = bool(user32.IsIconic(wintypes.HWND(h_val)))

                rect = wintypes.RECT()
                user32.GetWindowRect(wintypes.HWND(h_val), ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top

                proc_name = self._get_process_name_by_pid(pid.value)
                is_wechat_proc = proc_name in self.WECHAT_PROCESS_NAMES
                is_main_cls = cls_name in self.WECHAT_MAIN_CLASSES
                is_chat_cls = cls_name in self.WECHAT_CHAT_CLASSES
                is_qt_cls = "QWindowIcon" in cls_name or cls_name.startswith("mmui::")
                is_wx_title = self._is_wechat_title(title)

                if not (
                    is_main_cls
                    or is_chat_cls
                    or (is_wechat_proc and (is_qt_cls or is_wx_title or (is_visible and w >= 260 and h >= 260)))
                    or (is_qt_cls and is_wx_title)
                    or (is_wx_title and (is_visible or is_iconic))
                ):
                    return True

                if proc_name and not is_wechat_proc and not (is_main_cls or is_chat_cls or is_wx_title):
                    return True

                if not is_visible and not is_iconic:
                    if (is_main_cls or (is_wechat_proc and is_wx_title)) and w >= 200 and h >= 200:
                        candidates.append((25, h_val))
                    return True

                if is_visible and not is_iconic and (w < 200 or h < 200):
                    return True

                score = 100 if is_visible else 60
                if is_wechat_proc:
                    score += 300
                if is_main_cls:
                    score += 240
                elif is_chat_cls:
                    score += 200
                elif is_qt_cls:
                    score += 150
                if is_wx_title:
                    score += 220
                if w >= 350 and h >= 350:
                    score += 100
                if h_val == fg_hwnd:
                    score += 180
                if target_norm and title and (title == target_norm or target_norm in title):
                    score += 500

                candidates.append((score, h_val))
            except Exception:
                pass
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        except Exception:
            pass

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            self._hwnd = candidates[0][1]
            return self._hwnd

        for cls_name in self.WECHAT_CLASSES:
            hwnd = int(user32.FindWindowW(cls_name, None) or 0)
            if hwnd and (user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd)):
                self._hwnd = hwnd
                return self._hwnd

        for title_name in ("微信", "Weixin", "WeChat"):
            hwnd = int(user32.FindWindowW(None, title_name) or 0)
            if hwnd:
                self._hwnd = hwnd
                return self._hwnd

        return None

    def _get_dpi_scale(self, hwnd: Optional[int]) -> int:
        if sys.platform != "win32":
            return 100
        try:
            if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                dpi = ctypes.windll.user32.GetDpiForWindow(int(hwnd))
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

        from ctypes import wintypes
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        cls_name = buf.value
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 256)
        title = buf.value

        version_desc = (
            "4.x (Qt/mmui 架构)"
            if ("Qt" in cls_name or "mmui" in cls_name or "Weixin" in cls_name)
            else "3.9.x (Duilib 架构)"
        )
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
        hwnd = self._find_wechat_hwnd()
        if not hwnd:
            return ["文件传输助手"]
        try:
            with _uia_thread_scope():
                import uiautomation as auto
                wnd = auto.ControlFromHandle(hwnd)
                chats: List[str] = []
                for item in wnd.GetChildren():
                    name = (item.Name or "").strip()
                    if name and len(name) < 40 and name not in GENERIC_CHAT_TITLES:
                        chats.append(name)
                if "文件传输助手" not in chats:
                    chats.insert(0, "文件传输助手")
                return chats[:20]
        except Exception:
            return ["文件传输助手"]

    @staticmethod
    def _uia_descendants(root: object, max_depth: int = 12) -> Iterator[object]:
        """限制遍历开销，避免大型聊天记录拖慢每次发送校验。"""
        stack = deque([(root, 0)])
        remaining = 600
        deadline = time.monotonic() + 0.35
        while stack and remaining > 0 and time.monotonic() < deadline:
            node, depth = stack.popleft()
            remaining -= 1
            if depth:
                yield node
            if depth < max_depth:
                try:
                    stack.extend((child, depth + 1) for child in node.GetChildren())
                except Exception:
                    continue

    def get_current_chat_title(self) -> str:
        if sys.platform != "win32":
            return ""
        hwnd = self._find_wechat_hwnd()
        if not hwnd:
            return ""
        try:
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 256)
            win_title = (buf.value or "").strip()
            if win_title and not self._is_wechat_title(win_title) and win_title not in GENERIC_CHAT_TITLES:
                return win_title
        except Exception:
            pass

        try:
            with _uia_thread_scope():
                import uiautomation as auto
                wnd = auto.ControlFromHandle(hwnd)
                edit = wnd.EditControl(searchDepth=12)
                if edit.Exists(0, 0) and edit.Name:
                    candidate = edit.Name.strip()
                    if candidate and candidate not in GENERIC_CHAT_TITLES and not self._is_wechat_title(candidate):
                        return candidate
        except Exception:
            pass
        return "当前微信聊天"

    def _capture_ocr_area(self, area: str) -> Optional[Image.Image]:
        """Read only the foreground WeChat header or message area, in memory."""
        if sys.platform != "win32":
            return None
        try:
            hwnd = getattr(self, "_hwnd", None) or self._find_wechat_hwnd()
            if not hwnd or int(ctypes.windll.user32.GetForegroundWindow() or 0) != int(hwnd):
                return None
            from ctypes import wintypes
            from PIL import ImageGrab
            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
                return None
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < 300 or h < 300:
                return None
            if area == "header":
                box = (rect.left + int(w * .28), rect.top + 24, rect.right - int(w * .12), rect.top + min(140, int(h * .19)))
            elif area == "composer":
                box = (rect.left + int(w * .32), rect.top + int(h * .77), rect.right - 22, rect.top + int(h * .90))
            else:
                # Keep avatar glyphs out of the text line: OCR may otherwise
                # append them to an otherwise correct outgoing filename.
                box = (rect.left + int(w * .28), rect.top + int(h * .16), rect.right - max(64, int(w * .11)), rect.top + int(h * .73))
            return ImageGrab.grab(bbox=box, all_screens=True)
        except Exception:
            return None

    def _ocr_chat_title(self) -> str:
        image = self._capture_ocr_area("header")
        if image is None:
            return ""
        lines = recognize_local_text(image)
        if lines is None:
            return ""
        candidates = []
        for line in lines:
            value = "".join(line.text.split()).strip()
            if value and value not in GENERIC_CHAT_TITLES and not self._is_wechat_title(value):
                candidates.append((line.box[1], line.box[0], value))
        return sorted(candidates)[0][2] if candidates else ""

    def _count_outgoing_text_ocr(self, text: str) -> Optional[int]:
        image = self._capture_ocr_area("messages")
        if image is None:
            return None
        lines = recognize_local_text(image)
        if lines is None:
            return None
        target = normalized_text(text)
        matches = set()
        for line in lines:
            x, y, w, h = line.box
            if not (w > 0 and h > 0 and x + w >= image.width * .70
                    and x + w * .5 >= image.width * .52):
                continue
            if normalized_text(line.text) == target:
                matches.add(line.box)
                continue
            # Small client text benefits from a second OCR pass on its own
            # padded, enlarged line. The final comparison remains exact.
            if abs(len(normalized_text(line.text)) - len(target)) > max(2, len(target) // 3):
                continue
            for padding, scale in ((2, 2), (5, 3)):
                crop = image.crop((max(0, x - padding), max(0, y - padding),
                                   min(image.width, x + w + padding), min(image.height, y + h + padding)))
                enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
                refined = recognize_local_text(enlarged)
                if refined is not None and len(refined) == 1 and normalized_text(refined[0].text) == target:
                    matches.add(line.box)
                    break
        return len(matches)

    def search_and_switch_chat(self, chat_name: str) -> bool:
        """
        使用微信内置 Ctrl+F 搜索框自动寻址并切换到指定联系人/群聊：
        当开启 auto_search_chat 且当前会话与目标不一致时自动触发。
        """
        target = (chat_name or "").strip()
        if not target or target in GENERIC_CHAT_TITLES or sys.platform != "win32":
            return False
        try:
            self.clipboard.set_text(target)
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_F = 0x46
            VK_V = 0x56
            VK_RETURN = 0x0D
            KEYEVENTF_KEYUP = 0x0002

            # Ctrl + F 唤醒微信顶部搜索框
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_F, 0, 0, 0)
            user32.keybd_event(VK_F, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.18)

            # Ctrl + V 粘贴联系人名称
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_V, 0, 0, 0)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.32)

            # Enter 选中第一条匹配结果进入聊天
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.18)
            return True
        except Exception:
            return False

    def activate_chat(self, chat_name: str) -> bool:
        hwnd = self._find_wechat_hwnd(target_chat=chat_name)
        if not hwnd or sys.platform != "win32":
            return False
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        h_wnd = wintypes.HWND(int(hwnd))

        try:
            if user32.IsIconic(h_wnd):
                user32.ShowWindow(h_wnd, 9)
            elif not user32.IsWindowVisible(h_wnd):
                user32.ShowWindow(h_wnd, 5)
                user32.ShowWindow(h_wnd, 9)

            user32.BringWindowToTop(h_wnd)
            user32.SetForegroundWindow(h_wnd)

            if int(user32.GetForegroundWindow() or 0) != int(hwnd):
                fg_hwnd = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg_hwnd), None) if fg_hwnd else 0
                cur_tid = kernel32.GetCurrentThreadId()
                wx_tid = user32.GetWindowThreadProcessId(h_wnd, None)

                if fg_tid and fg_tid != wx_tid:
                    user32.AttachThreadInput(fg_tid, wx_tid, True)
                if cur_tid and cur_tid != wx_tid:
                    user32.AttachThreadInput(cur_tid, wx_tid, True)

                try:
                    user32.keybd_event(0x12, 0, 0, 0)
                    user32.keybd_event(0x12, 0, 0x0002, 0)
                    user32.BringWindowToTop(h_wnd)
                    user32.SetForegroundWindow(h_wnd)
                finally:
                    if fg_tid and fg_tid != wx_tid:
                        user32.AttachThreadInput(fg_tid, wx_tid, False)
                    if cur_tid and cur_tid != wx_tid:
                        user32.AttachThreadInput(cur_tid, wx_tid, False)

            deadline = time.monotonic() + 0.45
            while time.monotonic() < deadline:
                if int(user32.GetForegroundWindow() or 0) == int(hwnd):
                    break
                time.sleep(0.03)
            else:
                time.sleep(0.08)

            if self.enable_accessibility:
                self.activator.activate(hwnd)

            if self.auto_search_chat and chat_name and chat_name not in GENERIC_CHAT_TITLES:
                curr = self.get_current_chat_title()
                if curr in GENERIC_CHAT_TITLES or (chat_name not in curr and curr not in chat_name):
                    self.search_and_switch_chat(chat_name)
        except Exception:
            return False
        return True

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        hwnd = self._find_wechat_hwnd(target_chat=expected_chat)
        if not hwnd or sys.platform != "win32":
            return False, "未找到微信窗口，请确认微信已打开并登录"
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        fg = int(user32.GetForegroundWindow() or 0)

        if fg != int(hwnd):
            wx_pid = wintypes.DWORD(0)
            fg_pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(wx_pid))
            if fg:
                user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fg_pid))

            if fg == 0 or fg_pid.value == os.getpid() or fg_pid.value == wx_pid.value:
                self.activate_chat(expected_chat)
                fg = int(user32.GetForegroundWindow() or 0)
                if fg:
                    user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fg_pid))

            if fg != int(hwnd) and fg_pid.value != wx_pid.value:
                self.activate_chat(expected_chat)
                fg = int(user32.GetForegroundWindow() or 0)
                if fg:
                    user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fg_pid))
                if fg != int(hwnd) and fg_pid.value != wx_pid.value:
                    return False, "微信窗口已失去前台焦点，为防止误发已自动暂停"

        curr_title = (self.get_current_chat_title() or "").strip()
        exp_clean = (expected_chat or "").strip()
        if (
            exp_clean
            and exp_clean not in GENERIC_CHAT_TITLES
            and curr_title
            and curr_title not in GENERIC_CHAT_TITLES
            and exp_clean not in curr_title
            and curr_title not in exp_clean
        ):
            if self.auto_search_chat:
                self.search_and_switch_chat(exp_clean)
                curr_title = (self.get_current_chat_title() or "").strip()
            if (
                curr_title
                and curr_title not in GENERIC_CHAT_TITLES
                and exp_clean not in curr_title
                and curr_title not in exp_clean
            ):
                return False, f"检测到聊天窗口变化（当前：{curr_title}，目标：{exp_clean}），已暂停"
        return True, "OK"

    def _verification_context_ok(self) -> bool:
        """确认期间不抢回焦点；用户切走或更换聊天时立即暂停。"""
        if sys.platform != "win32" or not self._hwnd:
            return False
        if int(ctypes.windll.user32.GetForegroundWindow() or 0) != int(self._hwnd):
            return False
        return bool(self._expected_chat) and self.get_current_chat_title().strip() == self._expected_chat


    def _capture_chat_region(self) -> Image.Image:
        # A missing window, changed monitor/layout, or backend switch is unknown,
        # never a reason to compare the whole desktop against an outgoing image.
        if sys.platform != "win32":
            raise RuntimeError("当前环境不能读取微信聊天画面")
        hwnd = getattr(self, "_image_verify_hwnd", None) or getattr(self, "_hwnd", None) or self._find_wechat_hwnd()
        if not hwnd:
            raise RuntimeError("未定位到微信窗口，请重新刷新/定位聊天")
        frame = capture_chat_frame(int(hwnd), prefer_print_window=self.prefer_print_window)
        signature = getattr(self, "_capture_signature", None)
        backend = getattr(self, "_capture_backend", None)
        if signature is not None and signature != frame.signature:
            raise RuntimeError("微信窗口位置、缩放或输入区布局发生变化，请重新核实当前图片")
        if backend is not None and backend != frame.backend:
            raise RuntimeError("截图方式发生变化，当前图片需要重新核实")
        self._capture_signature = frame.signature
        self._capture_backend = frame.backend
        return frame.image

    def _assert_submission_foreground(self) -> None:
        hwnd = getattr(self, "_image_verify_hwnd", None) or getattr(self, "_hwnd", None)
        reader = ctypes.windll.user32.GetForegroundWindow
        reader.restype = ctypes.c_void_p
        if not hwnd or int(reader() or 0) != int(hwnd):
            raise RuntimeError("微信窗口已离开前台，已暂停；请先核实输入框及消息")

    def _send_keys_win32_fallback(self, send_shortcut: str, paste_wait: float, enter_wait: float) -> None:
        user32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_RETURN = 0x0D
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002

        self._assert_submission_foreground()
        if self._should_stop():
            raise RuntimeError("用户已暂停，未继续提交消息")
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        if self._wait_or_stop(paste_wait):
            raise RuntimeError("已暂停；内容可能仍在微信输入框，请先核实")
        self._assert_submission_foreground()

        if send_shortcut.upper() == "CTRL+ENTER":
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        else:
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(enter_wait)

    def _paste_and_submit(self, send_shortcut: str, is_text: bool = False) -> None:
        if sys.platform != "win32":
            raise RuntimeError("当前环境不能操作微信发送")
        if self._should_stop():
            raise RuntimeError("用户已暂停，未继续提交消息")
        paste_wait = 0.12 if is_text else 0.30
        enter_wait = 0.08
        try:
            import uiautomation as auto
        except ImportError:
            self._send_keys_win32_fallback(send_shortcut, paste_wait, enter_wait)
            return
        # Once a paste has been attempted, an exception can mean it already took
        # effect. Never replay the whole operation with another keyboard backend.
        with _uia_thread_scope():
            self._assert_submission_foreground()
            auto.SendKeys("{Ctrl}v", waitTime=paste_wait)
            if self._wait_or_stop(paste_wait):
                raise RuntimeError("已暂停；内容可能仍在微信输入框，请先核实")
            self._assert_submission_foreground()
            keys = "{Ctrl}{Enter}" if send_shortcut.upper() == "CTRL+ENTER" else "{Enter}"
            auto.SendKeys(keys, waitTime=enter_wait)

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        self._capture_signature = None
        self._capture_backend = None
        self._last_confirmed_path = None
        self._last_confirmed_frame = None
        self._last_locked_bbox = None
        self.last_verify_screenshot = None
        self.last_verify_bbox = None
        self.last_verify_status = None
        self._pre_send_shot = None
        self._image_verify_hwnd = getattr(self, "_hwnd", None) or self._find_wechat_hwnd()
        # Capture is required before submission, so historical images and draft
        # previews cannot substitute for evidence of this newly submitted image.
        self._pre_send_shot = self._capture_chat_region()
        self._pre_send_wal = None
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

    @staticmethod
    def _is_new_outgoing_bubble(
        pre_shot: Optional[Image.Image], curr_shot: Image.Image, bbox: Tuple[int, int, int, int]
    ) -> bool:
        if pre_shot is None or pre_shot.size != curr_shot.size:
            return False
        x, y, bw, bh = bbox
        width, _ = curr_shot.size
        if x + bw * 0.5 < width * 0.55 or x + bw < width * 0.75:
            return False
        box = (x, y, x + bw, y + bh)
        before = np.asarray(pre_shot.convert("RGB").crop(box), dtype=np.float32)
        after = np.asarray(curr_shot.convert("RGB").crop(box), dtype=np.float32)
        difference = np.abs(after - before)
        return float(np.mean(difference)) > 2.0 and float(np.mean(difference > 12.0)) > 0.06

    def verify_image_sent(
        self, image_path: Path, timeout_sec: float = 60.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        self._last_confirmed_path = None
        self._last_confirmed_frame = None
        if getattr(self, "_pre_send_shot", None) is None:
            return self._unconfirmed_status("缺少发送前聊天画面，请人工核实图片")
        src_img, _, _ = load_static_image(image_path)
        # A pre-existing copy can scroll to a new coordinate when the pending
        # copy is submitted. Coordinate differences cannot identify which one
        # finished uploading. Do not automatically name an ambiguous duplicate.
        try:
            existed, _, _ = self.verifier.locate_thumbnail(self._pre_send_shot, src_img)
        except Exception as exc:
            return self._unconfirmed_status(f"无法核实发送前图片记录：{exc}")
        if existed:
            return self._unconfirmed_status("发送前已存在同内容图片，无法区分新旧上传状态；请人工核实本张后继续")
        file_mb = image_path.stat().st_size / (1024.0 * 1024.0)
        timeout = max(float(timeout_sec), 30.0 + min(file_mb * 5.0, 270.0))
        quiet_required = max(1.5, float(stable_duration_sec), float(self.quiet_confirm_sec))
        started = time.monotonic()
        deadline = started + timeout
        previous = None
        quiet_since = None
        quiet_frames = 0
        stable_bbox = None
        locked_widths = None
        frame_index = 0
        status = self._unconfirmed_status("等待新图片上屏及上传指示消失")
        while time.monotonic() < deadline:
            if self._should_stop():
                return self._unconfirmed_status("用户已暂停图片确认，未发送名称")
            try:
                shot = self._capture_chat_region()
            except Exception as exc:
                return self._unconfirmed_status(f"无法可靠读取聊天画面：{exc}")
            self.last_verify_screenshot = shot
            if shot.info.get("capture_layout") == "fallback":
                return self._unconfirmed_status("无法可靠区分消息区与输入区，图片已提交，请人工核实后再补发名称")
            if shot.size != self._pre_send_shot.size:
                return self._unconfirmed_status("聊天区域尺寸发生变化，请人工核实图片")
            status = self.verifier.evaluate(shot, src_img, previous, candidate_widths=locked_widths)
            if not status.matched and locked_widths is not None:
                locked_widths = None
                status = self.verifier.evaluate(shot, src_img, previous, candidate_widths=None)
            self.last_verify_bbox = status.bbox
            now = time.monotonic()
            if status.has_failure_badge:
                status.is_upload_complete = False
                self.last_verify_status = status
                return status
            fresh = bool(status.bbox and self._is_new_outgoing_bubble(self._pre_send_shot, shot, status.bbox))
            ready = self.image_status_allows_name(status) and fresh
            if ready and now - started >= max(0.75, self.post_send_grace_sec):
                if quiet_since is None or not self._same_image_bbox(stable_bbox, status.bbox):
                    quiet_since, quiet_frames = now, 1
                    stable_bbox = status.bbox
                else:
                    quiet_frames += 1
                if quiet_frames >= 4 and now - quiet_since >= quiet_required:
                    status.reason = f"新图片匹配，转圈消失且连续稳定 {now - quiet_since:.1f} 秒"
                    self._last_confirmed_path = os.path.normcase(str(image_path.resolve()))
                    self._last_confirmed_frame = shot
                    self._last_locked_bbox = status.bbox
                    self.last_verify_status = status
                    return status
                status.reason = f"图片已匹配，等待无转圈连续稳定 {quiet_required:.1f} 秒"
            else:
                quiet_since, quiet_frames, stable_bbox = None, 0, None
                if status.matched and not fresh:
                    status.reason = "尚未确认是本次新发出的图片，等待或人工核实"
            # A per-frame classifier result is not yet the sequence-level barrier.
            status.is_upload_complete = False
            self.last_verify_status = status
            previous = shot
            locked_widths = [status.bbox[2]] if status.matched and status.bbox else None
            frame_index += 1
            if self._wait_or_stop(0.12 if frame_index % 2 else 0.16):
                return self._unconfirmed_status("用户已暂停图片确认，未发送名称")
        status.is_upload_complete = False
        status.reason = f"等待图片无转圈并稳定超时 ({timeout:.0f}s)：{status.reason}"
        self.last_verify_status = status
        return status

    @staticmethod
    def _same_image_bbox(first, second) -> bool:
        if first is None or second is None:
            return False
        ax, ay, aw, ah = first
        bx, by, bw, bh = second
        overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
        union = aw * ah + bw * bh - overlap
        return union > 0 and overlap / union >= 0.80

    def confirm_image_before_name(self, image_path: Path) -> Tuple[bool, str]:
        key = os.path.normcase(str(image_path.resolve()))
        previous = getattr(self, "_last_confirmed_frame", None)
        if self._should_stop():
            return False, "用户已暂停，未发送名称"
        if getattr(self, "_last_confirmed_path", None) != key or previous is None:
            return False, "缺少本轮图片确认画面，请先人工核实图片"
        try:
            shot = self._capture_chat_region()
            if shot.info.get("capture_layout") == "fallback":
                return False, "无法可靠区分消息区与输入区，请人工核实图片后再补发名称"
            src_img, _, _ = load_static_image(image_path)
            box = getattr(self, "_last_locked_bbox", None)
            status = self.verifier.evaluate(shot, src_img, previous, candidate_widths=[box[2]] if box else None)
        except Exception as exc:
            return False, f"名称发送前无法复查图片：{exc}"
        self.last_verify_screenshot, self.last_verify_bbox, self.last_verify_status = shot, status.bbox, status
        if (self.image_status_allows_name(status) and self._same_image_bbox(box, status.bbox)
                and self._is_new_outgoing_bubble(self._pre_send_shot, shot, status.bbox)):
            return True, "名称发送前图片复查通过"
        self._last_confirmed_path = None
        return False, f"名称发送前图片仍在变化或转圈：{status.reason}"

    def _count_outgoing_text_matches(self, text: str) -> Optional[int]:
        """只计聊天记录内右侧且内容完全一致的文本，排除输入框与搜索结果。"""
        if sys.platform != "win32":
            return None
        hwnd = self._find_wechat_hwnd()
        if not hwnd:
            return None
        try:
            with _uia_thread_scope():
                import uiautomation as auto
                wnd = auto.ControlFromHandle(hwnd)
                rect = wnd.BoundingRectangle
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width <= 0 or height <= 0:
                    return None
                matches = set()
                readable_message_area = False
                for node in self._uia_descendants(wnd):
                    kind = node.ControlTypeName
                    if kind in ("ListControl", "ListItemControl", "DocumentControl"):
                        area = node.BoundingRectangle
                        if (area.right > rect.left + width * 0.70
                                and area.top < rect.bottom - height * 0.20
                                and area.bottom > rect.top + height * 0.20):
                            readable_message_area = True
                    if kind not in ("TextControl", "ListItemControl") or (node.Name or "").strip() != text.strip():
                        continue
                    box = node.BoundingRectangle
                    if (box.right > box.left and box.bottom > box.top
                            and box.right - box.left <= width * 0.58
                            and (box.left + box.right) * 0.5 >= rect.left + width * 0.62
                            and box.right >= rect.left + width * 0.72
                            and box.top >= rect.top + height * 0.15
                            and box.bottom <= rect.bottom - height * 0.16):
                        matches.add((box.left, box.top, box.right, box.bottom))
                if readable_message_area or matches:
                    return len(matches)
        except Exception:
            pass
        return self._count_outgoing_text_ocr(text)

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        if self._should_stop():
            return False, "用户已暂停，未提交名称"
        self.clipboard.set_text(text)
        self._paste_and_submit(send_shortcut, is_text=True)
        if self._wait_or_stop(0.45):
            return False, "名称提交后暂停，请人工核实是否已发出"
        # Keep the original keyboard submission path, without an OCR dependency.
        return True, f"已提交图片名称：{text}"

    def close(self) -> None:
        self.activator.restore()
