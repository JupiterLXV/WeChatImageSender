from __future__ import annotations

import ctypes
import io
import time
from ctypes import wintypes
from pathlib import Path

from .core import SenderError
from .images import load_rgb

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.OpenClipboard.argtypes = [wintypes.HWND]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


class SingleInstance:
    """Prevent two senders from sharing the clipboard and active chat."""

    def __init__(self, demo=False):
        self.handle = kernel32.CreateMutexW(None, False, 'Local\\WeChatImageSender' + ('.demo' if demo else ''))
        error = ctypes.get_last_error()
        if not self.handle:
            raise SenderError('无法创建程序运行锁。')
        if error == 183:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise SenderError('微信图片顺序发送助手已在运行，请使用已有窗口。')

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def dpi_aware():
    try:
        ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise SenderError('无法读取微信进程信息，请以相同权限运行微信和本程序。')
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise SenderError('无法验证微信进程。')
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise SenderError('无法核对微信登录进程。')
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise SenderError('无法核对微信进程创建时间。')
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return f'{pid}:{created}'
    finally:
        kernel32.CloseHandle(handle)


def version_of(path: str) -> str:
    version = ctypes.WinDLL('version', use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return '未知'
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return '未知'
    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, '\\', ctypes.byref(ptr), ctypes.byref(length)):
        return '未知'
    values = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD))
    ms, ls = values[2], values[3]
    return f'{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}'


def clipboard(data: bytes, format_id: int):
    for attempt in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise SenderError('剪贴板正被其他程序占用，请稍后重试。')
    handle = None
    try:
        handle = kernel32.GlobalAlloc(0x0002, len(data))
        if not handle:
            raise SenderError('无法为图片分配剪贴板内存。')
        address = kernel32.GlobalLock(handle)
        if not address:
            raise SenderError('无法写入剪贴板。')
        try:
            ctypes.memmove(address, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.EmptyClipboard() or not user32.SetClipboardData(format_id, handle):
            raise SenderError('剪贴板写入失败。')
        handle = None  # Ownership transferred to Windows.
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def copy_text(text: str):
    clipboard((text + '\0').encode('utf-16-le'), 13)


def copy_image(path: Path):
    image = load_rgb(path)
    stream = io.BytesIO()
    image.save(stream, 'BMP')
    clipboard(stream.getvalue()[14:], 8)
