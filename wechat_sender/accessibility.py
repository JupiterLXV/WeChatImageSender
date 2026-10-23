# -*- coding: utf-8 -*-
"""
零内存补丁（Zero-Memory-Patch）微信无障碍激活器：
通过标准 Windows API `SystemParametersInfoW(SPI_SETSCREENREADER)` 与 `WM_GETOBJECT` 信号
唤醒微信 Qt/Chromium 界面的 `QAccessible` 控件树，彻底告别对 `Weixin.dll` 的单版本 SHA-256 内存修改。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from typing import Optional

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
WM_GETOBJECT = 0x003D
# Win32 OBJID_CLIENT = (LONG)0xFFFFFFFC = -4（使用有符号 -4 避免 64 位 Python ctypes 32位 c_int 溢出报错）
OBJID_CLIENT = -4
SMTO_ABORTIFHUNG = 0x0002


@dataclass
class AccessibilityState:
    """记录无障碍激活前后的系统状态，用于退出时自动恢复。"""
    previously_enabled: bool
    activated_by_helper: bool
    wechat_hwnd: Optional[int] = None


class ZeroPatchAccessibilityActivator:
    """
    安全唤醒微信 3.x / 4.x 无障碍控件树：
    1. 设置进程环境变量 QT_ACCESSIBILITY=1；
    2. 按需通过 SystemParametersInfoW 开启临时屏幕阅读器标志并向微信窗口发送 WM_GETOBJECT；
    3. 退出时自动恢复原系统标志，完全不触碰 Weixin.dll 进程内存。
    """

    def __init__(self) -> None:
        self._state: Optional[AccessibilityState] = None

    @staticmethod
    def get_screen_reader_flag() -> bool:
        if sys.platform != "win32":
            return False
        try:
            flag = ctypes.c_int(0)
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETSCREENREADER, 0, ctypes.byref(flag), 0
            )
            return bool(flag.value)
        except Exception:
            return False

    def activate(self, hwnd: Optional[int] = None) -> AccessibilityState:
        os.environ["QT_ACCESSIBILITY"] = "1"
        os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"

        if sys.platform != "win32":
            self._state = AccessibilityState(
                previously_enabled=False,
                activated_by_helper=True,
                wechat_hwnd=hwnd,
            )
            return self._state

        prev = self.get_screen_reader_flag()
        original_flag = self._state.previously_enabled if self._state is not None else prev
        changed_by_helper = bool(self._state and self._state.activated_by_helper) or not prev
        try:
            if not prev:
                # fWinIni=0 避免全局广播 WM_SETTINGCHANGE 造成其他无响应窗口阻塞
                ctypes.windll.user32.SystemParametersInfoW(
                    SPI_SETSCREENREADER, 1, None, 0
                )
        except Exception:
            pass

        if hwnd:
            try:
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                lresult = ctypes.c_ssize_t(0)
                user32.SendMessageTimeoutW.argtypes = [
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                    wintypes.UINT,
                    wintypes.UINT,
                    ctypes.POINTER(ctypes.c_ssize_t),
                ]
                user32.SendMessageTimeoutW.restype = wintypes.LPARAM
                user32.SendMessageTimeoutW(
                    wintypes.HWND(int(hwnd)),
                    WM_GETOBJECT,
                    0,
                    OBJID_CLIENT,
                    SMTO_ABORTIFHUNG,
                    300,
                    ctypes.byref(lresult),
                )
            except Exception:
                pass

        self._state = AccessibilityState(
            previously_enabled=original_flag,
            activated_by_helper=changed_by_helper,
            wechat_hwnd=hwnd,
        )
        return self._state

    def restore(self) -> None:
        if self._state is None:
            return
        if sys.platform == "win32" and self._state.activated_by_helper and not self._state.previously_enabled:
            try:
                ctypes.windll.user32.SystemParametersInfoW(
                    SPI_SETSCREENREADER, 0, None, 0
                )
            except Exception:
                pass
        self._state = None
