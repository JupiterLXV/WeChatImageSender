"""Window-owned mouse input for Qt's transparent rendering surface."""
import ctypes
import time
from ctypes import wintypes

from .core import NeedsReview, Stopped
from .winutil import user32

_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [wintypes.HWND, _callback, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND


def click_point(hwnd, pid, x, y, right=False, stop=None):
    if stop is not None and stop.is_set():
        raise Stopped('已停止，尚未操作微信。')
    if user32.GetForegroundWindow() != hwnd:
        raise NeedsReview('微信失去前台焦点，未执行鼠标操作。')
    renders = []
    def collect(handle, unused):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, name, 256)
        if name.value in {'MMUIRenderSubWindow', 'MMUIRenderSubWindowHW'}:
            renders.append(handle)
        return True
    collect(hwnd, 0)
    user32.EnumChildWindows(hwnd, _callback(collect), 0)
    saved = []
    try:
        for handle in renders:
            style = user32.GetWindowLongPtrW(handle, -20)
            if style & 0x20:
                saved.append((handle, style))
                user32.SetWindowLongPtrW(handle, -20, style & ~0x20)
        point = wintypes.POINT(round(x), round(y))
        hit = user32.WindowFromPoint(point)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hit, ctypes.byref(owner))
        if owner.value != pid or user32.GetForegroundWindow() != hwnd:
            raise NeedsReview('目标位置被其他窗口遮挡，未执行鼠标操作。')
        if stop is not None and stop.is_set():
            raise Stopped('已停止。')
        user32.SetCursorPos(point.x, point.y)
        user32.mouse_event(8 if right else 2, 0, 0, 0, 0)
        user32.mouse_event(16 if right else 4, 0, 0, 0, 0)
        time.sleep(0.1)
    finally:
        for handle, style in reversed(saved):
            user32.SetWindowLongPtrW(handle, -20, style)


def click_control(window, control, right=False, stop=None):
    rect = control.BoundingRectangle
    if control.IsOffscreen or rect.width() <= 0 or rect.height() <= 0:
        raise NeedsReview('微信目标控件当前不可见。')
    click_point(window.NativeWindowHandle, window.ProcessId,
                (rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2, right, stop)
