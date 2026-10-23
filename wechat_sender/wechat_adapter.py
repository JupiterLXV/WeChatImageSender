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
from .windows_ocr import normalized_text, recognize as recognize_local_text
from .image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    BubbleVisualStatus,
    VisualBubbleVerifier,
    load_static_image,
)

DEFAULT_QUIET_CONFIRM_SEC = 0.35

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
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
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
        self.quiet_confirm_sec = max(0.10, min(5.0, sec))

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
            sx1 = max(0, bx - 42)
            sx2 = max(0, bx - 12)
            if sx2 > sx1:
                draw.rectangle((sx1, by, sx2, by + bh), outline=(245, 175, 45), width=2)
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

    @staticmethod
    def _capture_via_print_window(hwnd: int, w: int, h: int) -> Optional[Image.Image]:
        """
        使用 Win32 PrintWindow(PW_RENDERFULLCONTENT=2) 直接读取微信窗口显存缓冲区：
        即使屏幕右下角弹出系统通知或有置顶悬浮窗，也不会遮挡微信聊天画面。
        若目标显卡驱动对 Qt/Chromium 返回全黑位图（标准差 < 1.0），自动返回 None 交由 ImageGrab 兜底。
        """
        if sys.platform != "win32" or w < 200 or h < 200:
            return None
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        h_wnd = wintypes.HWND(int(hwnd))
        wnd_dc = user32.GetWindowDC(h_wnd)
        if not wnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(wnd_dc)
        if not mem_dc:
            user32.ReleaseDC(h_wnd, wnd_dc)
            return None
        bmp = gdi32.CreateCompatibleBitmap(wnd_dc, w, h)
        if not bmp:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(h_wnd, wnd_dc)
            return None
        old_obj = gdi32.SelectObject(mem_dc, bmp)
        try:
            # PW_RENDERFULLCONTENT = 0x00000002
            ok = user32.PrintWindow(h_wnd, mem_dc, 2)
            if not ok:
                return None
            bmi = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            lines = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, bmi, 0)
            if lines != h:
                return None
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
            crop = img.crop((int(w * 0.28), int(h * 0.15), w - 6, h - int(h * 0.16)))
            if float(np.std(np.asarray(crop, dtype=np.float32))) < 1.0:
                return None
            return crop
        except Exception:
            return None
        finally:
            gdi32.SelectObject(mem_dc, old_obj)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(h_wnd, wnd_dc)

    def _capture_chat_region(self) -> Image.Image:
        from PIL import ImageGrab
        hwnd = self._find_wechat_hwnd()
        if hwnd and sys.platform == "win32":
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect))
            w = max(200, rect.right - rect.left)
            h = max(200, rect.bottom - rect.top)
            if self.prefer_print_window:
                pw_img = self._capture_via_print_window(int(hwnd), w, h)
                if pw_img is not None:
                    return pw_img
            box = (
                rect.left + int(w * 0.28),
                rect.top + int(h * 0.15),
                rect.right - 6,
                rect.bottom - int(h * 0.16),
            )
            return ImageGrab.grab(bbox=box, all_screens=True)
        return ImageGrab.grab(all_screens=True)

    @staticmethod
    def _send_keys_win32_fallback(send_shortcut: str, paste_wait: float, enter_wait: float) -> None:
        user32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_RETURN = 0x0D
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002

        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(paste_wait)

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
            return
        paste_wait = 0.05 if is_text else 0.08
        enter_wait = 0.04 if is_text else 0.06
        try:
            with _uia_thread_scope():
                import uiautomation as auto
                auto.SendKeys("{Ctrl}v", waitTime=paste_wait)
                time.sleep(paste_wait)
                if send_shortcut.upper() == "CTRL+ENTER":
                    auto.SendKeys("{Ctrl}{Enter}", waitTime=enter_wait)
                else:
                    auto.SendKeys("{Enter}", waitTime=enter_wait)
        except Exception:
            self._send_keys_win32_fallback(send_shortcut, paste_wait, enter_wait)

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
            self.last_verify_screenshot = curr_shot

            status = self.verifier.evaluate(
                curr_shot,
                src_img,
                prev_shot,
                candidate_widths=locked_widths,
            )

            if not status.matched and locked_widths is not None:
                locked_widths = None
                status = self.verifier.evaluate(curr_shot, src_img, prev_shot, candidate_widths=None)

            if status.matched and status.bbox is not None and status.has_static_spinner and prev_shot is not None:
                live_motion = self.verifier.compute_spinner_motion(prev_shot, curr_shot, status.bbox)
                if live_motion <= self.verifier.motion_quiet_threshold:
                    status = BubbleVisualStatus(
                        matched=True,
                        similarity=status.similarity,
                        bbox=status.bbox,
                        has_failure_badge=status.has_failure_badge,
                        has_static_spinner=False,
                        spinner_motion_score=live_motion,
                        is_upload_complete=not status.has_failure_badge,
                        reason=f"缩略图匹配 ({status.similarity:.2f}) 且状态区画面已静止 (差分 {live_motion:.2f})",
                    )

            if status.matched and status.bbox is not None:
                self._last_locked_bbox = status.bbox
                self.last_verify_bbox = status.bbox
                locked_widths = [status.bbox[2]]

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
                    self.last_verify_bbox = diff_bbox

            if status.has_static_spinner or (status.matched and status.spinner_motion_score > self.verifier.motion_quiet_threshold):
                spinner_seen = True

            if (now - send_start_ts) < self.post_send_grace_sec:
                status.is_upload_complete = False
                quiet_since = None

            last_status = status
            self.last_verify_status = status
            if status.has_failure_badge:
                return status

            if status.matched and status.is_upload_complete:
                if quiet_since is None:
                    quiet_since = now
                else:
                    elapsed_quiet = now - quiet_since
                    curr_wal = self.wal_tracker.snapshot()
                    wal_pulsed = (self._pre_send_wal is not None and curr_wal is not None and curr_wal != self._pre_send_wal)
                    needed_sec = 0.12 if spinner_seen else (min(0.18, target_quiet_sec) if wal_pulsed else target_quiet_sec)
                    if elapsed_quiet >= needed_sec:
                        return status
            else:
                quiet_since = None

            prev_shot = curr_shot
            time.sleep(0.06)

        last_status.is_upload_complete = False
        last_status.reason = f"等待图片上传完成超时 ({adaptive_timeout:.0f}s): {last_status.reason}"
        self.last_verify_status = last_status
        return last_status

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
        try:
            prev_txt_shot = self._capture_chat_region()
        except Exception:
            prev_txt_shot = None
        pre_txt_wal = self.wal_tracker.snapshot()

        self.clipboard.set_text(text)
        self._paste_and_submit(send_shortcut, is_text=True)

        send_ts = time.monotonic()
        deadline = send_ts + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.06)
            if prev_txt_shot is None:
                return True, f"已发送图片名称：{text}"
            try:
                after_text_shot = self._capture_chat_region()
                if after_text_shot.size == prev_txt_shot.size:
                    w, h = after_text_shot.size
                    b1 = np.asarray(prev_txt_shot.convert("L").crop((int(w * 0.15), int(h * 0.35), w, h)), dtype=np.float32)
                    b2 = np.asarray(after_text_shot.convert("L").crop((int(w * 0.15), int(h * 0.35), w, h)), dtype=np.float32)
                    abs_d = np.abs(b2 - b1)
                    if float(np.mean(abs_d)) > 0.20 or int(np.sum(abs_d > 12.0)) >= 80:
                        return True, f"已确认图片名称“{text}”极速上屏完成"
                curr_wal = self.wal_tracker.snapshot()
                if pre_txt_wal is not None and curr_wal is not None and curr_wal != pre_txt_wal:
                    return True, f"已确认图片名称“{text}”写入完成"
                if (time.monotonic() - send_ts) >= 0.45:
                    return True, f"已发送图片名称：{text}"
            except Exception:
                return True, f"已发送图片名称：{text}"

        return True, f"已发送图片名称：{text}"

    def close(self) -> None:
        self.activator.restore()
