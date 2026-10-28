# -*- coding: utf-8 -*-
"""
微信图片顺序发送助手：桌面布局、任务状态与 Windows 窗口兼容。
核心 UI、算法与交互特性：
1. 蓝白灰桌面视觉规范，支持浅色与深色主题切换；
2. 系统级全局热键（Win32 RegisterHotKey F8）：即使微信处于前台，按 F8 也能请求停止后续发送；
3. 发送期间右上角置顶迷你悬浮窗（Mini HUD）：实时显示发送进度、当前文件名与一键暂停按钮；
4. 待核实项弹窗内置「原图 vs 微信现场实拍（带定位框标注）」并排视觉对比，无需切窗口即可秒做决策；
5. 原生支持 Windows 文件/文件夹拖拽导入（WM_DROPFILES）、多选批量改名模板（如 {num:02d}_{name}）、
   后台异步流式扫描、队列状态筛选胶囊 + 搜索框 + 点击表头排序、预览图自适应缩放与双击高清大图查看器；
6. 内置多维微信窗口枚举补丁（EnumWindows + 进程名校验 + 尺寸评分 + 前台焦点解锁 + 2.5s 自动重连探测）。
"""

import atexit
import contextlib
import ctypes
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - 兼容无 GUI 的 Headless CI 环境
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

from .gui_layout import DesktopLayoutMixin, THEMES
from .gui_workflow import SenderWorkflowMixin

from .image_utils import (
    DEFAULT_SIMILARITY_THRESHOLD,
    ImageItem,
    format_batch_display_name,
    load_static_image,
    natural_sort_key,
    scan_image_folder,
    scan_image_paths,
)
from .job_store import (
    ItemStage,
    JobRecordItem,
    JobState,
    ManualDecision,
    SequentialSendEngine,
)
from .wechat_adapter import (
    DEFAULT_QUIET_CONFIRM_SEC,
    BaseWeChatAdapter,
    DemoWeChatAdapter,
    WindowsWeChatAdapter,
)

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

IGNORED_WINDOW_CLASSES = {
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


@contextlib.contextmanager
def _uia_thread_scope() -> Iterator[None]:
    """确保在后台线程调用 uiautomation 时正确初始化当前线程的 Windows COM 环境。"""
    if sys.platform != "win32":
        yield
        return
    try:
        import uiautomation as auto
        initializer = getattr(auto, "UIAutomationInitializerInThread", None)
    except Exception:
        initializer = None
    if initializer is not None:
        with initializer():
            yield
        return
    yield


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


def _is_wechat_window_title(title: str) -> bool:
    t = (title or "").strip()
    if not t or "微信图片顺序发送助手" in t or "WeChatImageSender" in t:
        return False
    if t in ("微信", "Weixin", "WeChat"):
        return True
    for prefix in ("微信(", "微信 (", "微信（", "Weixin(", "Weixin (", "WeChat(", "WeChat ("):
        if t.startswith(prefix):
            return True
    return False


def _install_robust_window_detector(adapter: BaseWeChatAdapter) -> None:
    """
    为 WindowsWeChatAdapter 注入多维窗口枚举与焦点自愈补丁：
    即使单独替换 gui.py 文件，也能彻底解决微信窗口无法识别、隐藏 0x0 窗口抢占句柄及焦点丢失问题。
    """
    if not isinstance(adapter, WindowsWeChatAdapter):
        return

    def _robust_find_wechat_hwnd(self: WindowsWeChatAdapter, target_chat: Optional[str] = None) -> Optional[int]:
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
                if not cls_name or cls_name in IGNORED_WINDOW_CLASSES or cls_name.startswith("Tk"):
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

                proc_name = _get_process_name_by_pid(pid.value)
                is_wechat_proc = proc_name in WECHAT_PROCESS_NAMES
                is_main_cls = cls_name in WECHAT_MAIN_CLASSES
                is_chat_cls = cls_name in WECHAT_CHAT_CLASSES
                is_qt_cls = "QWindowIcon" in cls_name or cls_name.startswith("mmui::")
                is_wx_title = _is_wechat_window_title(title)

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

        for cls_name in list(WECHAT_MAIN_CLASSES) + list(WECHAT_CHAT_CLASSES):
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

    def _robust_get_current_chat_title(self: WindowsWeChatAdapter) -> str:
        if sys.platform != "win32":
            return ""
        hwnd = self._find_wechat_hwnd()
        if not hwnd:
            return ""
        try:
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(wintypes.HWND(int(hwnd)), buf, 256)
            win_title = (buf.value or "").strip()
            if win_title and not _is_wechat_window_title(win_title) and win_title not in GENERIC_CHAT_TITLES:
                return win_title
        except Exception:
            pass

        try:
            with _uia_thread_scope():
                import uiautomation as auto
                wnd = auto.ControlFromHandle(int(hwnd))
                edit = wnd.EditControl(searchDepth=12)
                if edit.Exists(0, 0) and edit.Name:
                    candidate = edit.Name.strip()
                    if candidate and candidate not in GENERIC_CHAT_TITLES and not _is_wechat_window_title(candidate):
                        return candidate
        except Exception:
            pass
        return "当前微信聊天"

    def _robust_activate_chat(self: WindowsWeChatAdapter, chat_name: str) -> bool:
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

            if getattr(self, "enable_accessibility", True):
                self.activator.activate(hwnd)

            if getattr(self, "auto_search_chat", False) and chat_name and chat_name not in GENERIC_CHAT_TITLES:
                curr = self.get_current_chat_title()
                if curr in GENERIC_CHAT_TITLES or (chat_name not in curr and curr not in chat_name):
                    if hasattr(self, "search_and_switch_chat"):
                        self.search_and_switch_chat(chat_name)
        except Exception:
            return False
        return True

    def _robust_ensure_foreground_and_target(self: WindowsWeChatAdapter, expected_chat: str) -> Tuple[bool, str]:
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
            if getattr(self, "auto_search_chat", False) and hasattr(self, "search_and_switch_chat"):
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


    adapter._find_wechat_hwnd = types.MethodType(_robust_find_wechat_hwnd, adapter)  # type: ignore[method-assign]
    adapter.get_current_chat_title = types.MethodType(_robust_get_current_chat_title, adapter)  # type: ignore[method-assign]
    adapter.activate_chat = types.MethodType(_robust_activate_chat, adapter)  # type: ignore[method-assign]
    adapter.ensure_foreground_and_target = types.MethodType(_robust_ensure_foreground_and_target, adapter)  # type: ignore[method-assign]



STAGE_BADGES: Dict[str, str] = {
    ItemStage.PENDING.value: "待发送",
    ItemStage.IMAGE_SUBMITTED.value: "确认图片中",
    ItemStage.IMAGE_CONFIRMED.value: "图片已确认",
    ItemStage.NAME_SUBMITTED.value: "确认名称中",
    ItemStage.COMPLETED.value: "已完成",
    ItemStage.NEEDS_MANUAL_VERIFY.value: "待人工核实",
}


class _LegacySenderAppGUI:
    def __init__(self, root: Any, demo_mode: bool = False) -> None:
        if tk is None:
            raise RuntimeError("当前 Python 环境未安装 tkinter，请使用 --demo --headless 或在 Windows 标准 Python 下运行")
        self.root = root
        self.demo_mode = demo_mode
        self.theme_name = "light"
        self.colors = THEMES[self.theme_name]

        title_suffix = " [演示模式]" if demo_mode else " v3.6.5"
        self.root.title(f"微信图片顺序发送助手{title_suffix}")
        self.root.geometry("1200x810")
        self.root.minsize(1040, 700)

        self.adapter: BaseWeChatAdapter = (
            DemoWeChatAdapter(similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD)
            if demo_mode
            else WindowsWeChatAdapter(enable_accessibility=True, similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD)
        )
        _install_robust_window_detector(self.adapter)

        atexit.register(self.adapter.close)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.engine = SequentialSendEngine(self.adapter, on_progress=self._on_engine_progress)
        self.scanned_items: List[ImageItem] = []
        self.custom_names: Dict[int, str] = {}
        self.current_job: Optional[JobState] = None
        self.worker_thread: Optional[threading.Thread] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._preview_photo: Any = None
        self._lightbox_photo: Any = None
        self._verify_orig_photo: Any = None
        self._verify_live_photo: Any = None
        self._current_preview_idx: int = 0
        self._preview_resize_after_id: Optional[str] = None
        self._run_start_ts: Optional[float] = None
        self._closing = False
        self._hotkey_thread_id: Optional[int] = None
        self._mini_hud: Optional[Any] = None
        self._mini_hud_label: Optional[Any] = None
        self._wndproc_ref: Any = None
        self._old_wndproc: int = 0
        self._sort_col: str = "idx"
        self._sort_reverse: bool = False

        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self._build_ui()
        self._apply_theme()
        self.root.bind("<F8>", lambda _e: self.request_stop())
        self._start_global_hotkey_listener()
        self.root.after(200, self._setup_windows_drag_and_drop)
        self._refresh_connection_async()
        self._schedule_auto_detect()
        self._append_log(
            "系统就绪：已就绪。原文件发送、逐张状态确认、F8 暂停与任务恢复可用。"
        )


    # ==================== 系统级全局 F8 热键监听器 ====================
    def _start_global_hotkey_listener(self) -> None:
        """
        使用 Win32 RegisterHotKey 注册系统级全局 F8 热键：
        解决发送期间微信窗口位于前台、本程序无焦点导致按 F8 无法停止的问题。
        """
        if sys.platform != "win32":
            return

        def _hotkey_loop() -> None:
            try:
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                self._hotkey_thread_id = int(kernel32.GetCurrentThreadId())
                # ID=1, MOD_NOREPEAT=0x4000, VK_F8=0x77
                if not user32.RegisterHotKey(None, 1, 0x4000, 0x77):
                    # 兼容不支持 MOD_NOREPEAT 的环境
                    user32.RegisterHotKey(None, 1, 0, 0x77)
                msg = wintypes.MSG()
                while not self._closing:
                    ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret <= 0:
                        break
                    if msg.message == 0x0312:  # WM_HOTKEY
                        self.root.after(0, self.request_stop)
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                user32.UnregisterHotKey(None, 1)
            except Exception:
                pass

        t = threading.Thread(target=_hotkey_loop, daemon=True, name="GlobalF8Hotkey")
        t.start()

    def _stop_global_hotkey_listener(self) -> None:
        if sys.platform == "win32" and self._hotkey_thread_id:
            try:
                # WM_QUIT = 0x0012
                ctypes.windll.user32.PostThreadMessageW(int(self._hotkey_thread_id), 0x0012, 0, 0)
            except Exception:
                pass

    # ==================== Windows 原生文件/文件夹拖放 (WM_DROPFILES) ====================
    def _setup_windows_drag_and_drop(self) -> None:
        """通过 Win32 Shell32.DragAcceptFiles 与窗口过程钩子，支持直接从资源管理器拖入文件夹或多张图片。"""
        if sys.platform != "win32" or self._closing:
            return
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            hwnd = int(self.root.winfo_id())
            top_hwnd = int(user32.GetParent(wintypes.HWND(hwnd)) or hwnd)
            shell32.DragAcceptFiles(wintypes.HWND(top_hwnd), True)

            WM_DROPFILES = 0x0233
            GWL_WNDPROC = -4
            LRESULT = ctypes.c_ssize_t
            WNDPROC_TYPE = ctypes.WINFUNCTYPE(
                LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )

            set_wnd_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            call_wnd_proc = user32.CallWindowProcW
            call_wnd_proc.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            call_wnd_proc.restype = LRESULT

            def _py_wndproc(h_wnd: int, msg: int, wparam: int, lparam: int) -> int:
                if msg == WM_DROPFILES:
                    try:
                        h_drop = wintypes.HANDLE(wparam)
                        count = int(shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0))
                        dropped_paths: List[Path] = []
                        for i in range(count):
                            buf = ctypes.create_unicode_buffer(1024)
                            shell32.DragQueryFileW(h_drop, i, buf, 1024)
                            if buf.value:
                                dropped_paths.append(Path(buf.value))
                        shell32.DragFinish(h_drop)
                        if dropped_paths:
                            self.root.after(0, lambda p=dropped_paths: self._on_files_dropped(p))
                        return 0
                    except Exception:
                        pass
                return int(call_wnd_proc(self._old_wndproc, h_wnd, msg, wparam, lparam))

            self._wndproc_ref = WNDPROC_TYPE(_py_wndproc)
            set_wnd_long.restype = ctypes.c_void_p
            set_wnd_long.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC_TYPE]
            self._old_wndproc = int(set_wnd_long(wintypes.HWND(top_hwnd), GWL_WNDPROC, self._wndproc_ref) or 0)
        except Exception:
            pass

    def _on_files_dropped(self, paths: Sequence[Path]) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送进行中无法导入新文件，请先按 F8 暂停。")
            return
        if len(paths) == 1 and paths[0].is_dir():
            self.folder_var.set(str(paths[0]))
            self.rescan_folder()
            return

        base_dir = paths[0].parent if paths else Path.cwd()
        self.folder_var.set(str(base_dir))
        self._run_scan_from_paths(paths, base_dir)

    # ==================== 发送期间右上角置顶迷你悬浮条 (Mini HUD) ====================

    def _update_mini_hud(self, text: str) -> None:
        if self._mini_hud is not None and self._mini_hud_label is not None:
            try:
                self._mini_hud_label.configure(text=text[:42])
            except Exception:
                pass

    def _hide_mini_hud(self) -> None:
        if self._mini_hud is not None:
            try:
                self._mini_hud.destroy()
            except Exception:
                pass
            self._mini_hud = None
            self._mini_hud_label = None

    def _schedule_auto_detect(self) -> None:
        """空闲时每 2.5 秒自动静默检测微信窗口连接状态，无需手动点击刷新。"""
        if self._closing:
            return
        try:
            if not (self.worker_thread and self.worker_thread.is_alive()):
                diag = self.adapter.diagnose()
                if diag.wechat_running:
                    mode_txt = "演示模式" if self.demo_mode else f"已连接微信 ({diag.dpi_scale_percent}% DPI)"
                    self._set_connection_pill(True, f"● {mode_txt}")
                else:
                    self._set_connection_pill(False, "○ 未检测到微信窗口 (点击重试)")
        except Exception:
            pass
        if not self._closing:
            self.root.after(2500, self._schedule_auto_detect)




    def toggle_theme(self) -> None:
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self._apply_theme()


    @staticmethod
    def _stage_to_tag(stage: str) -> str:
        if stage == ItemStage.COMPLETED.value:
            return "done"
        if stage == ItemStage.NEEDS_MANUAL_VERIFY.value:
            return "verify"
        if stage in (
            ItemStage.IMAGE_SUBMITTED.value,
            ItemStage.IMAGE_CONFIRMED.value,
            ItemStage.NAME_SUBMITTED.value,
        ):
            return "active"
        return "pending"

    def _update_kpi_dashboard(self) -> None:
        if self.current_job:
            total = len(self.current_job.items)
            done = sum(1 for it in self.current_job.items if it.stage == ItemStage.COMPLETED.value)
            verify = sum(1 for it in self.current_job.items if it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value)
        else:
            total = len(self.scanned_items)
            done = 0
            verify = 0

        pct = (done * 100.0 / total) if total > 0 else 0.0
        self.progress_var.set(pct)
        self.kpi_val_labels["total"].configure(text=f"{total} 张")
        self.kpi_val_labels["done"].configure(text=f"{done} 张")
        self.kpi_val_labels["verify"].configure(text=f"{verify} 张")

        if self._run_start_ts and done > 0:
            elapsed = max(0.1, time.monotonic() - self._run_start_ts)
            avg_s = elapsed / done
            self.kpi_val_labels["speed"].configure(text=f"{pct:.0f}% ({avg_s:.2f}s/张)")
        else:
            self.kpi_val_labels["speed"].configure(text=f"{pct:.0f}%")

    def _on_params_change(self) -> None:
        try:
            val = float(self.threshold_var.get())
            self.adapter.set_similarity_threshold(max(0.05, min(0.95, val)))
        except Exception:
            pass
        try:
            q_sec = float(self.quiet_sec_var.get())
            self.adapter.set_quiet_confirm_sec(max(1.5, min(5.0, q_sec)))
        except Exception:
            pass
        try:
            self.adapter.set_auto_search_chat(self.auto_search_var.get())
            self.adapter.set_prefer_print_window(self.print_win_var.get())
        except Exception:
            pass

    def _set_connection_pill(self, connected: bool, label: str) -> None:
        self._wechat_connected = connected
        self._wechat_connection_text = label
        c = self.colors
        prefix = "connected" if connected else "disconnected"
        self.conn_pill.configure(text=label, bg=c[f"{prefix}_bg"], fg=c[f"{prefix}_fg"])

    def refresh_chats(self) -> None:
        diag = self.adapter.diagnose()
        if diag.wechat_running:
            mode_txt = "演示模式" if self.demo_mode else f"已连接微信 ({diag.dpi_scale_percent}% DPI)"
            self._set_connection_pill(True, f"● {mode_txt}")
        else:
            self._set_connection_pill(False, "○ 未检测到微信窗口 (点击重试)")

        chats = self.adapter.list_recent_chats()
        self.chat_combo["values"] = chats
        if chats and not self.chat_combo.get():
            self.chat_combo.set(chats[0])

    def use_current_chat(self) -> None:
        self.refresh_chats()
        diag = self.adapter.diagnose()
        if not diag.wechat_running:
            self.status_var.set("未检测到运行中的微信窗口，请先打开并登录微信。")
            self._append_log("未检测到微信窗口，请确认微信桌面版已打开且未完全退出。")
            return
        curr = self.adapter.get_current_chat_title() or "当前微信聊天"
        self.chat_combo.set(curr)
        self.status_var.set(f"已锁定当前微信会话：{curr}")
        self._append_log(f"已锁定微信当前聊天窗口：{curr} (句柄: {diag.window_handle})")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择图片文件夹")
        if chosen:
            self.folder_var.set(chosen)
            self.rescan_folder()

    def choose_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="选择多张要顺序发送的图片",
            filetypes=[("支持的图片", "*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff")],
        )
        if chosen:
            paths = [Path(p) for p in chosen]
            base = paths[0].parent
            self.folder_var.set(str(base))
            self._run_scan_from_paths(paths, base)

    # ==================== 异步后台流式文件夹扫描 ====================
    def rescan_folder(self, sync: bool = False) -> None:
        folder_str = self.folder_var.get().strip()
        if not folder_str:
            return
        folder_path = Path(folder_str)
        if not folder_path.exists() or not folder_path.is_dir():
            messagebox.showerror("路径无效", f"文件夹不存在：{folder_path}")
            return
        self._run_scan_from_paths([folder_path], folder_path, sync=sync)

    def _run_scan_from_paths(self, paths: Sequence[Path], base_folder: Path, sync: bool = False) -> None:
        inc_sub = self.subfolders_var.get()
        inc_ext = self.ext_var.get()
        allow_anim = (self.clip_mode_var.get() == "CF_HDROP")

        if sync:
            try:
                items, skipped = scan_image_paths(
                    paths=paths,
                    base_folder=base_folder,
                    include_subfolders=inc_sub,
                    include_extension=inc_ext,
                    allow_animated_in_file_mode=allow_anim,
                )
                self._finish_scan_results(items, skipped)
            except Exception as exc:
                messagebox.showerror("扫描失败", str(exc))
            return

        self.status_var.set("正在后台高速扫描图片并计算防篡改摘要...")

        def _worker() -> None:
            try:
                def _on_prog(cur: int, tot: int, _it: ImageItem) -> None:
                    if cur % 15 == 0 or cur == tot:
                        self.root.after(0, lambda c=cur, t=tot: self.status_var.set(f"正在扫描图片 ({c}/{t})..."))

                items, skipped = scan_image_paths(
                    paths=paths,
                    base_folder=base_folder,
                    include_subfolders=inc_sub,
                    include_extension=inc_ext,
                    allow_animated_in_file_mode=allow_anim,
                    progress_callback=_on_prog,
                )
                self.root.after(0, lambda: self._finish_scan_results(items, skipped))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): messagebox.showerror("扫描失败", err))

        self._scan_thread = threading.Thread(target=_worker, daemon=True)
        self._scan_thread.start()

    def _finish_scan_results(self, items: List[ImageItem], skipped: List[Tuple[Path, str]]) -> None:
        self.scanned_items = items
        self.custom_names.clear()
        self.current_job = None
        self._refresh_tree_view()
        self._update_kpi_dashboard()
        msg = f"已扫描 {len(items)} 张有效图片"
        if skipped:
            msg += f"（已跳过 {len(skipped)} 个不支持的文件）"
        self.status_var.set(msg)
        self._append_log(msg)
        if items:
            self.tree.selection_set("0")
            self._show_preview_for_index(0)

    def _get_effective_display_name(self, idx: int, item: ImageItem) -> str:
        return self.custom_names.get(idx, item.display_name)

    # ==================== 队列表格筛选、搜索与列排序 ====================
    def _sort_tree_by_column(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._refresh_tree_view()

    def _row_matches_filter(self, rel_name: str, send_name: str, stage: str) -> bool:
        flt = self.filter_stage_var.get()
        if flt == "PENDING" and stage != ItemStage.PENDING.value:
            return False
        if flt == "COMPLETED" and stage != ItemStage.COMPLETED.value:
            return False
        if flt == "VERIFY" and stage != ItemStage.NEEDS_MANUAL_VERIFY.value:
            return False

        q = self.search_query_var.get().strip().lower()
        if q and (q not in rel_name.lower() and q not in send_name.lower()):
            return False
        return True

    def _refresh_tree_view(self) -> None:
        if self.current_job:
            self._refresh_tree_from_job()
        else:
            self._refresh_tree_from_scan()

    def _refresh_tree_from_scan(self) -> None:
        self.tree.delete(*self.tree.get_children())
        rows = []
        for idx, it in enumerate(self.scanned_items):
            send_name = self._get_effective_display_name(idx, it)
            stage = ItemStage.PENDING.value
            if not self._row_matches_filter(it.relative_name, send_name, stage):
                continue
            badge = STAGE_BADGES.get(stage, stage)
            detail = f"{it.width}×{it.height} ({it.format_name})"
            rows.append((idx, it.relative_name, send_name, badge, detail, "pending"))

        rows = self._apply_row_sorting(rows)
        for idx, rel_name, send_name, badge, detail, tag in rows:
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(idx + 1, rel_name, send_name, badge, detail),
                tags=(tag,),
            )

    def _refresh_tree_from_job(self) -> None:
        if not self.current_job:
            return
        self.tree.delete(*self.tree.get_children())
        rows = []
        for it in self.current_job.items:
            if not self._row_matches_filter(it.relative_name, it.display_name, it.stage):
                continue
            badge = STAGE_BADGES.get(it.stage, it.stage)
            tag = self._stage_to_tag(it.stage)
            rows.append((it.index, it.relative_name, it.display_name, badge, it.last_message, tag))

        rows = self._apply_row_sorting(rows)
        for idx, rel_name, send_name, badge, detail, tag in rows:
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(idx + 1, rel_name, send_name, badge, detail),
                tags=(tag,),
            )
        self._update_kpi_dashboard()

    def _apply_row_sorting(
        self, rows: List[Tuple[int, str, str, str, str, str]]
    ) -> List[Tuple[int, str, str, str, str, str]]:
        col = self._sort_col
        rev = self._sort_reverse
        if col == "idx":
            return sorted(rows, key=lambda r: r[0], reverse=rev)
        if col == "rel_name":
            return sorted(rows, key=lambda r: natural_sort_key(r[1]), reverse=rev)
        if col == "send_name":
            return sorted(rows, key=lambda r: natural_sort_key(r[2]), reverse=rev)
        if col == "stage":
            return sorted(rows, key=lambda r: r[3], reverse=rev)
        if col == "detail":
            return sorted(rows, key=lambda r: r[4], reverse=rev)
        return rows

    def _on_tree_select(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            self._current_preview_idx = idx
            self._show_preview_for_index(idx)
        except Exception:
            pass

    def _on_preview_canvas_configure(self, _event: Any = None) -> None:
        """当窗口拉伸改变预览画布大小时，防抖 60ms 自动重绘高清缩略图。"""
        if self._preview_resize_after_id:
            try:
                self.root.after_cancel(self._preview_resize_after_id)
            except Exception:
                pass
        self._preview_resize_after_id = self.root.after(
            60, lambda: self._show_preview_for_index(self._current_preview_idx)
        )



    def _open_lightbox_viewer(self) -> None:
        """双击右侧缩略图弹出高清大图查看器（Lightbox）。"""
        file_path, display_name, meta_str = self._get_item_path_and_name(self._current_preview_idx)
        if not file_path or not file_path.exists() or ImageTk is None:
            return
        try:
            rgb_img, _, _ = load_static_image(file_path)
        except Exception as exc:
            messagebox.showerror("无法打开大图", str(exc))
            return

        top = tk.Toplevel(self.root)
        top.title(f"高清大图查看器 - {display_name} ({file_path.name})")
        top.geometry("860x660")
        top.configure(bg=self.colors["card_bg"])
        top.transient(self.root)

        lbl_info = tk.Label(
            top,
            text=f"{display_name}  |  {meta_str.replace(chr(10), '  |  ')}",
            bg=self.colors["card_bg"],
            fg=self.colors["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
            pady=8,
        )
        lbl_info.pack(fill=tk.X)

        canvas = tk.Canvas(top, bg=self.colors["preview_bg"], highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        def _render_large(_e: Any = None) -> None:
            w = max(320, canvas.winfo_width() - 24)
            h = max(240, canvas.winfo_height() - 24)
            copy_img = rgb_img.copy()
            copy_img.thumbnail((w, h), Image.Resampling.LANCZOS)
            self._lightbox_photo = ImageTk.PhotoImage(copy_img)
            canvas.delete("all")
            canvas.create_image(canvas.winfo_width() // 2, canvas.winfo_height() // 2, image=self._lightbox_photo, anchor=tk.CENTER)

        canvas.bind("<Configure>", _render_large)

    def apply_custom_name(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        new_name = self.edit_name_var.get().strip()
        if not new_name:
            messagebox.showwarning("名称为空", "发送的图片名称不能为空。")
            return

        if self.current_job and 0 <= idx < len(self.current_job.items):
            self.current_job.items[idx].display_name = new_name
            self.engine.save_job(self.current_job)
        elif 0 <= idx < len(self.scanned_items):
            self.custom_names[idx] = new_name

        self._refresh_tree_view()
        if self.tree.exists(str(idx)):
            self.tree.selection_set(str(idx))
        self._append_log(f"已将第 {idx + 1} 张图片的待发送名称修改为：“{new_name}”")

    def apply_batch_template(self) -> None:
        """将批量命名模板套用到当前多选的行（若只选了1行或未选，则套用到全部队列）。"""
        tpl = self.batch_tpl_var.get().strip()
        if not tpl:
            messagebox.showwarning("模板为空", "请输入有效的命名模板，例如 {num:02d}_{name}")
            return

        sel = [int(s) for s in self.tree.selection()]
        inc_ext = self.ext_var.get()

        if self.current_job:
            target_indices = sel if len(sel) > 1 else list(range(len(self.current_job.items)))
            for seq_num, idx in enumerate(target_indices, start=1):
                rec = self.current_job.items[idx]
                rec.display_name = format_batch_display_name(tpl, seq_num, Path(rec.file_path), inc_ext)
            self.engine.save_job(self.current_job)
            count = len(target_indices)
        elif self.scanned_items:
            target_indices = sel if len(sel) > 1 else list(range(len(self.scanned_items)))
            for seq_num, idx in enumerate(target_indices, start=1):
                it = self.scanned_items[idx]
                self.custom_names[idx] = format_batch_display_name(tpl, seq_num, it.path, inc_ext)
            count = len(target_indices)
        else:
            return

        self._refresh_tree_view()
        self._append_log(f"已按模板“{tpl}”批量更新 {count} 张图片的待发送名称。")

    def move_selected(self, delta: int) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送进行中无法调整顺序，请先按 F8 停止。")
            return
        sel = self.tree.selection()
        if not sel or not self.scanned_items or self.current_job is not None:
            return
        idx = int(sel[0])
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.scanned_items)):
            return
        self.scanned_items[idx], self.scanned_items[new_idx] = (
            self.scanned_items[new_idx],
            self.scanned_items[idx],
        )
        n1 = self.custom_names.get(idx)
        n2 = self.custom_names.get(new_idx)
        if n2 is not None:
            self.custom_names[idx] = n2
        else:
            self.custom_names.pop(idx, None)
        if n1 is not None:
            self.custom_names[new_idx] = n1
        else:
            self.custom_names.pop(new_idx, None)

        self._refresh_tree_view()
        if self.tree.exists(str(new_idx)):
            self.tree.selection_set(str(new_idx))
            self.tree.see(str(new_idx))

    def remove_selected(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送进行中无法移除条目，请先按 F8 停止。")
            return
        sel = sorted((int(s) for s in self.tree.selection()), reverse=True)
        if not sel or not self.scanned_items or self.current_job is not None:
            return

        removed_Set = set(sel)
        old_items = self.scanned_items
        new_items: List[ImageItem] = []
        new_custom: Dict[int, str] = {}
        for old_i, it in enumerate(old_items):
            if old_i in removed_Set:
                continue
            new_i = len(new_items)
            new_items.append(it)
            if old_i in self.custom_names:
                new_custom[new_i] = self.custom_names[old_i]

        self.scanned_items = new_items
        self.custom_names = new_custom
        self._refresh_tree_view()
        self._update_kpi_dashboard()
        self._append_log(f"已从队列批量移除 {len(removed_Set)} 项。")

    def _on_engine_progress(self, item: JobRecordItem, message: str) -> None:
        def _update() -> None:
            iid = str(item.index)
            badge = STAGE_BADGES.get(item.stage, item.stage)
            tag = self._stage_to_tag(item.stage)
            if self.tree.exists(iid):
                self.tree.item(
                    iid,
                    values=(item.index + 1, item.relative_name, item.display_name, badge, message),
                    tags=(tag,),
                )
                self.tree.see(iid)
                self.tree.selection_set(iid)
            self._show_preview_for_index(item.index)
            self._update_kpi_dashboard()
            total = len(self.current_job.items) if self.current_job else len(self.scanned_items)
            self._update_mini_hud(f"🚀 [{item.index + 1}/{total}] {item.display_name} · {badge}")
            self.status_var.set(f"[{item.index + 1}] {item.display_name}: {message}")
            self._append_log(f"[#{item.index + 1} {item.display_name}] {message}")

        self.root.after(0, _update)

    def start_or_resume(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送任务正在运行中。")
            return

        self._on_params_change()
        self.refresh_chats()
        target_chat = self.chat_combo.get().strip()
        if not target_chat:
            messagebox.showwarning("缺少目标会话", "请先选择或填写微信接收对象。")
            return

        if self.current_job is None:
            if not self.scanned_items:
                messagebox.showwarning("列表为空", "请先选择包含有效图片的文件夹或拖入图片。")
                return
            final_items = [
                ImageItem(
                    path=it.path,
                    relative_name=it.relative_name,
                    display_name=self._get_effective_display_name(i, it),
                    sha256=it.sha256,
                    width=it.width,
                    height=it.height,
                    format_name=it.format_name,
                    is_mpo_primary=it.is_mpo_primary,
                )
                for i, it in enumerate(self.scanned_items)
            ]
            self.current_job = self.engine.create_job(
                folder_path=Path(self.folder_var.get().strip() or "."),
                image_items=final_items,
                target_chat=target_chat,
                clipboard_mode=self.clip_mode_var.get(),
                send_shortcut=self.shortcut_var.get(),
                include_extension=self.ext_var.get(),
                include_subfolders=self.subfolders_var.get(),
            )
            self._refresh_tree_from_job()

        self._run_start_ts = time.monotonic()
        if self.hud_enable_var.get():
            self._show_mini_hud()
        self.adapter.activate_chat(target_chat)
        self._append_log(
            f"开始执行发送队列 → 目标会话：“{target_chat}” | 阈值={self.adapter.similarity_threshold:.2f} | 静默={self.adapter.quiet_confirm_sec:.2f}s"
        )

        def _run() -> None:
            assert self.current_job is not None
            ok, summary = self.engine.run_job(
                self.current_job,
                stable_duration_sec=self.adapter.quiet_confirm_sec,
            )
            self.root.after(0, self._hide_mini_hud)
            self.root.after(0, self._update_kpi_dashboard)
            self.root.after(0, lambda: self.status_var.set(summary))
            self.root.after(0, lambda: self._append_log(summary))
            if not ok:
                self.root.after(0, lambda: messagebox.showwarning("队列已暂停", summary))
            else:
                self.root.after(0, lambda: messagebox.showinfo("发送完成", summary))

        self.worker_thread = threading.Thread(target=_run, daemon=True)
        self.worker_thread.start()

    def request_stop(self) -> None:
        self.engine.stop_requested = True
        self._update_mini_hud("正在安全停止队列…")
        self.status_var.set("已发出全局停止指令 (F8)，正在安全暂停当前步骤...")
        self._append_log("收到停止指令 (F8)，正在安全暂停...")

    # ==================== 待核实项弹窗（内置原图 vs 微信现场实拍并排对比） ====================
    def handle_needs_verify(self) -> None:
        if not self.current_job:
            messagebox.showinfo("提示", "当前没有活动的任务记录。")
            return
        pending_items = [it for it in self.current_job.items if it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value]
        if not pending_items:
            messagebox.showinfo("提示", "当前没有需要人工核实的条目。")
            return

        target = pending_items[0]
        dlg = tk.Toplevel(self.root)
        dlg.title(f"处理待核实项 - 第 {target.index + 1} 张 ({target.display_name})")
        dlg.geometry("720x480")
        dlg.configure(bg=self.colors["card_bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text=(
                f"文件：{target.relative_name}   |   待发名称：{target.display_name}\n"
                f"暂停原因：{target.last_message}"
            ),
            style="CardTitle.TLabel",
            wraplength=680,
        ).pack(padx=18, pady=(12, 6), anchor=tk.W)

        # 并排现场视觉对比区（左侧：本地待发原图；右侧：暂停瞬间微信右下角实拍 + 绿/黄识别框）
        compare_frame = tk.Frame(dlg, bg=self.colors["card_bg"])
        compare_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=4)
        compare_frame.columnconfigure(0, weight=1)
        compare_frame.columnconfigure(1, weight=1)

        left_box = tk.Frame(compare_frame, bg=self.colors["preview_bg"], highlightthickness=1, highlightbackground=self.colors["card_border"])
        left_box.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 6))
        tk.Label(left_box, text="本地待发送原图", bg=self.colors["preview_bg"], fg=self.colors["text"], font=("Microsoft YaHei UI", 9, "bold")).pack(pady=4)
        orig_lbl = tk.Label(left_box, bg=self.colors["preview_bg"], text="无原图预览", fg=self.colors["text_muted"])
        orig_lbl.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        right_box = tk.Frame(compare_frame, bg=self.colors["preview_bg"], highlightthickness=1, highlightbackground=self.colors["card_border"])
        right_box.grid(row=0, column=1, sticky=tk.NSEW, padx=(6, 0))
        tk.Label(
            right_box,
            text="暂停时微信画面（蓝框：图片，黄框：状态）",
            bg=self.colors["preview_bg"],
            fg=self.colors["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(pady=4)
        live_lbl = tk.Label(right_box, bg=self.colors["preview_bg"], text="暂无微信现场截图", fg=self.colors["text_muted"])
        live_lbl.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        if ImageTk is not None:
            try:
                p_orig = Path(target.file_path)
                if p_orig.exists():
                    im_o, _, _ = load_static_image(p_orig)
                    im_o.thumbnail((300, 185), Image.Resampling.LANCZOS)
                    self._verify_orig_photo = ImageTk.PhotoImage(im_o)
                    orig_lbl.configure(image=self._verify_orig_photo, text="")
            except Exception:
                pass

            try:
                annotated = self.adapter.get_annotated_verify_screenshot()
                if annotated is not None:
                    im_l = annotated.copy()
                    im_l.thumbnail((300, 185), Image.Resampling.LANCZOS)
                    self._verify_live_photo = ImageTk.PhotoImage(im_l)
                    live_lbl.configure(image=self._verify_live_photo, text="")
            except Exception:
                pass

        def _apply(decision: ManualDecision) -> None:
            assert self.current_job is not None
            self.engine.apply_manual_decision(self.current_job, target.index, decision)
            self._refresh_tree_from_job()
            self._append_log(f"人工核实处理第 {target.index + 1} 张 ({target.display_name}) -> {decision.value}")
            dlg.destroy()

        btn_frame = tk.Frame(dlg, bg=self.colors["card_bg"])
        btn_frame.pack(fill=tk.X, padx=18, pady=(6, 12))

        ttk.Button(
            btn_frame,
            text="1. 图片已成功发出 → 仅补发图片名称",
            style="Primary.TButton",
            command=lambda: _apply(ManualDecision.SEND_NAME_ONLY),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            btn_frame,
            text="2. 图片与名称均已发出 → 标记本张完成",
            style="Secondary.TButton",
            command=lambda: _apply(ManualDecision.MARK_COMPLETED),
        ).pack(fill=tk.X, pady=3)
        ttk.Button(
            btn_frame,
            text="3. 确认未发出（且已清理草稿）→ 重新发送本张",
            style="Secondary.TButton",
            command=lambda: _apply(ManualDecision.RETRY_FULL_ITEM),
        ).pack(fill=tk.X, pady=3)

    def load_history_job(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择历史任务 JSON 记录",
            initialdir=str(self.engine.jobs_dir),
            filetypes=[("Job JSON", "*.json")],
        )
        if not chosen:
            return
        try:
            job = self.engine.load_job(Path(chosen))
            self.current_job = job
            self.folder_var.set(job.folder_path)
            self.chat_combo.set(job.target_chat)
            self.clip_mode_var.set(job.clipboard_mode)
            self.shortcut_var.set(job.send_shortcut)
            self._refresh_tree_from_job()
            msg = f"已加载历史任务：{job.job_id}（共 {len(job.items)} 项）"
            self.status_var.set(msg)
            self._append_log(msg)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))


class SenderAppGUI(SenderWorkflowMixin, DesktopLayoutMixin, _LegacySenderAppGUI):
    """Desktop UI sharing the tested sequential sending workflow."""
    pass
