"""Temporary Qt accessibility activation for one fingerprinted official build.

Only the single accessibility-active byte is touched. No executable code,
installation files, message storage or credentials are read or modified.
Unknown binaries are rejected; every byte changed here is restored on exit.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .core import NeedsReview

VERIFIED_BUILDS = {
    ('4.1.15.53', '88eabf78f961e0abe26b397521b05f4ba6cd752727fb9db74a2b3f660b0400f2'): 0x0B3959D8,
}


def activation_offset(version: str, digest: str) -> int:
    try:
        return VERIFIED_BUILDS[(version, digest)]
    except KeyError as exc:
        raise NeedsReview('当前微信构建的辅助功能接口尚未验证，已停止。请更新发送助手以适配该构建。') from exc


class ModuleEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD),
                ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.c_void_p),
                ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE),
                ('szModule', wintypes.WCHAR * 256), ('szExePath', wintypes.WCHAR * 260)]


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
_kernel.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel.OpenProcess.restype = wintypes.HANDLE
for function in (_kernel.ReadProcessMemory, _kernel.WriteProcessMemory):
    function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    function.restype = wintypes.BOOL
_leases = {}
_lock = threading.Lock()


def _module(pid):
    snapshot = _kernel.CreateToolhelp32Snapshot(0x18, pid)
    if snapshot == ctypes.c_void_p(-1).value:
        raise NeedsReview('无法核对微信辅助功能模块。请以相同权限运行微信和发送助手。')
    try:
        entry = ModuleEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = _kernel.Module32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szModule.lower() == 'weixin.dll':
                return int(entry.modBaseAddr), int(entry.modBaseSize), Path(entry.szExePath)
            more = _kernel.Module32NextW(snapshot, ctypes.byref(entry))
        raise NeedsReview('没有找到微信主进程的 Weixin.dll。')
    finally:
        _kernel.CloseHandle(snapshot)


def _read_byte(handle, address):
    value = ctypes.c_ubyte()
    count = ctypes.c_size_t()
    if not _kernel.ReadProcessMemory(handle, address, ctypes.byref(value), 1, ctypes.byref(count)) or count.value != 1:
        raise NeedsReview('无法读取已验证的微信辅助功能标记。')
    return value.value


def _write_byte(handle, address, value):
    data = ctypes.c_ubyte(value)
    count = ctypes.c_size_t()
    if not _kernel.WriteProcessMemory(handle, address, ctypes.byref(data), 1, ctypes.byref(count)) or count.value != 1:
        raise NeedsReview('无法设置已验证的微信辅助功能标记。')


def _present(hwnd):
    import uiautomation as uia
    root = uia.ControlFromHandle(hwnd)
    queue = [(root, 0)] if root else []
    checked = 0
    while queue and checked < 400:
        item, depth = queue.pop(0)
        checked += 1
        # Qt can retain the root's class after the flag was restored to zero;
        # that cached root alone says nothing about newly created menu nodes.
        if item.ClassName in {'mmui::MainWindow', 'mmui::LoginWindow'}:
            return True
        if depth < 30 and item.ControlTypeName != 'ListControl':
            queue.extend((child, depth + 1) for child in item.GetChildren())
    return False


def enable(pid: int, hwnd: int, version: str):
    with _lock:
        base, size, path = _module(pid)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        offset = activation_offset(version, digest)
        if not 0 <= offset < size:
            raise NeedsReview('辅助功能标记超出已验证模块范围。')
        address = base + offset
        key = (pid, base)
        if key in _leases:
            handle, leased_address, _ = _leases[key]
            if _read_byte(handle, leased_address) == 1 and _present(hwnd):
                return
            raise NeedsReview('辅助功能曾启用但当前控件不可读取，请重启发送助手后重新检测。')
        handle = _kernel.OpenProcess(0x438, False, pid)
        if not handle:
            raise NeedsReview('无法启用微信辅助功能接口。请以相同权限运行两个程序。')
        changed = False
        keep = False
        try:
            original = _read_byte(handle, address)
            if original not in (0, 1):
                raise NeedsReview('辅助功能标记不是已验证的布尔值，未作修改。')
            if original == 0:
                _write_byte(handle, address, 1)
                changed = True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _present(hwnd):
                    if changed:
                        _leases[key] = (handle, address, original)
                        keep = True
                    return
                time.sleep(0.1)
            raise NeedsReview('辅助功能接口未公开微信控件，已恢复原标记并停止。')
        finally:
            if not keep:
                try:
                    if changed:
                        _write_byte(handle, address, original)
                finally:
                    _kernel.CloseHandle(handle)


def restore_all():
    with _lock:
        for handle, address, original in _leases.values():
            try:
                if _read_byte(handle, address) == 1:
                    _write_byte(handle, address, original)
            except NeedsReview:
                pass
            finally:
                _kernel.CloseHandle(handle)
        _leases.clear()


atexit.register(restore_all)
