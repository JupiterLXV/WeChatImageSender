"""Validated WeChat message-area capture, without reading chat titles.

The composer is excluded from the captured area.  Callers must treat a change
in ``signature`` or ``backend`` during a submission as an unknown result.
"""
from __future__ import annotations

import contextlib
import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional, Tuple

import numpy as np
from PIL import Image, ImageGrab, ImageStat

Box = Tuple[int, int, int, int]


class CaptureUnavailableError(RuntimeError):
    """The target message area cannot currently be captured reliably."""


@dataclass(frozen=True)
class ChatFrame:
    image: Image.Image
    signature: tuple
    backend: str


@dataclass(frozen=True)
class _Geometry:
    window: Box
    client: Box
    dpi: int


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("planes", ctypes.c_uint16),
        ("bit_count", ctypes.c_uint16), ("compression", ctypes.c_uint32),
        ("size_image", ctypes.c_uint32), ("xppm", ctypes.c_int32),
        ("yppm", ctypes.c_int32), ("colors_used", ctypes.c_uint32),
        ("colors_important", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("color", ctypes.c_uint32)]


@lru_cache(maxsize=1)
def _load_win32() -> Tuple[Any, Any]:
    if sys.platform != "win32":
        raise CaptureUnavailableError("聊天画面捕获仅支持 Windows。")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    handle = ctypes.c_void_p
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        function = getattr(user32, name)
        function.argtypes, function.restype = [handle], wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], handle
    for name in ("GetWindowRect", "GetClientRect"):
        function = getattr(user32, name)
        function.argtypes, function.restype = [handle, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL
    user32.ClientToScreen.argtypes = [handle, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    if hasattr(user32, "GetDpiForWindow"):
        user32.GetDpiForWindow.argtypes, user32.GetDpiForWindow.restype = [handle], wintypes.UINT
    if hasattr(user32, "SetThreadDpiAwarenessContext"):
        user32.SetThreadDpiAwarenessContext.argtypes = [handle]
        user32.SetThreadDpiAwarenessContext.restype = handle
    user32.GetDC.argtypes, user32.GetDC.restype = [handle], handle
    user32.ReleaseDC.argtypes, user32.ReleaseDC.restype = [handle, handle], ctypes.c_int
    user32.PrintWindow.argtypes, user32.PrintWindow.restype = [handle, handle, wintypes.UINT], wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [handle], handle
    gdi32.CreateDIBSection.argtypes = [handle, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                     ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = handle
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [handle, handle], handle
    gdi32.DeleteObject.argtypes, gdi32.DeleteObject.restype = [handle], wintypes.BOOL
    gdi32.DeleteDC.argtypes, gdi32.DeleteDC.restype = [handle], wintypes.BOOL
    return user32, gdi32


@contextlib.contextmanager
def _physical_coordinates(user32: Any):
    previous = None
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is not None:
        try:
            previous = setter(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            pass
    try:
        yield
    finally:
        if previous and setter is not None:
            try:
                setter(previous)
            except OSError:
                pass


def _geometry(user32: Any, hwnd: int) -> _Geometry:
    if not hwnd or not user32.IsWindow(hwnd):
        raise CaptureUnavailableError("微信窗口已关闭，无法确认图片。")
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        raise CaptureUnavailableError("微信窗口不可见或已最小化，已暂停图片确认。")
    if int(user32.GetForegroundWindow() or 0) != int(hwnd):
        raise CaptureUnavailableError("微信窗口不在前台，已暂停图片确认。")
    outer, client = wintypes.RECT(), wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
        raise CaptureUnavailableError("无法读取微信窗口位置。")
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise CaptureUnavailableError("无法读取微信聊天区域大小。")
    width, height = client.right - client.left, client.bottom - client.top
    if min(outer.right - outer.left, outer.bottom - outer.top, width, height) < 400:
        raise CaptureUnavailableError("微信聊天窗口过小，请扩大窗口后继续。")
    origin = wintypes.POINT(client.left, client.top)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise CaptureUnavailableError("无法确定微信聊天区域的屏幕位置。")
    dpi_reader = getattr(user32, "GetDpiForWindow", None)
    dpi = int(dpi_reader(hwnd) or 96) if dpi_reader is not None else 96
    if not 48 <= dpi <= 960:
        raise CaptureUnavailableError("微信窗口显示缩放异常，无法确认图片。")
    return _Geometry(
        (outer.left, outer.top, outer.right, outer.bottom),
        (origin.x, origin.y, origin.x + width, origin.y + height), dpi,
    )


def _uia_message_box(hwnd: int, client: Box, dpi: int) -> Optional[Box]:
    """Best-effort geometry only: no nickname, OCR or message text is needed."""
    try:
        import uiautomation as auto
        initializer = getattr(auto, "UIAutomationInitializerInThread", None)
        scope = initializer() if initializer is not None else contextlib.nullcontext()
        with scope:
            root = auto.ControlFromHandle(hwnd)
            left, top, right, bottom = client
            width, height = right - left, bottom - top
            edits, messages = [], []
            stack, remaining = [(root, 0)], 120
            deadline = time.monotonic() + .12
            while stack and remaining and time.monotonic() < deadline:
                node, depth = stack.pop()
                remaining -= 1
                kind = getattr(node, "ControlTypeName", "")
                box = getattr(node, "BoundingRectangle", None)
                if box is not None and not getattr(node, "IsOffscreen", False):
                    x1, y1, x2, y2 = box.left, box.top, box.right, box.bottom
                    if (x2 > x1 and y2 > y1 and x1 >= left - 4 and x2 <= right + 4
                            and y1 >= top - 4 and y2 <= bottom + 4
                            and x2 >= left + width * .78 and x2 - x1 >= width * .28):
                        if (kind in ("EditControl", "DocumentControl")
                                and top + height * .35 <= y1 < top + height * .90
                                and y2 >= top + height * .82
                                and y2 - y1 >= max(round(60 * dpi / 96), height * .10)):
                            edits.append(y1 - top)
                        if (kind in ("ListControl", "DocumentControl")
                                and top + height * .04 <= y1 <= top + height * .45
                                and top + height * .40 <= y2 <= top + height * .86
                                and y2 - y1 >= height * .25):
                            messages.append((x1 - left, y1 - top, x2 - left, y2 - top))
                # Message items do not help locate the container or composer.
                if depth < 6 and kind not in ("ListControl", "ListItemControl"):
                    stack.extend((child, depth + 1) for child in list(node.GetChildren())[:64])
            edit_limit = min(edits) - round(36 * dpi / 96) if edits else None
            if messages:
                chosen = max(messages, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                x1, y1, x2, y2 = chosen
                if edit_limit is not None:
                    y2 = min(y2, edit_limit)
                return max(0, x1), max(0, y1), min(width, x2), min(height, y2)
            if edit_limit is not None:
                return 0, round(height * .12), width, edit_limit
    except Exception:
        pass
    return None


def _find_input_divider(image: Image.Image, dpi: int) -> Optional[int]:
    """Find a thin, near-uniform separator across most of the chat width.

    Pick the uppermost credible line. A false positive above the composer can
    only reduce the inspected region; it must not extend into a draft preview.
    """
    width, height = image.size
    left, right = round(width * .32), width - max(6, round(6 * dpi / 96))
    if right - left < 160:
        return None
    pixels = np.asarray(image.convert("L"), dtype=np.float32)[:, left:right]
    gap = max(2, round(2 * dpi / 96))
    for y in range(max(gap, round(height * .42)), min(height - gap, round(height * .84))):
        row, above, below = pixels[y], pixels[y - gap], pixels[y + gap]
        if float(np.std(row)) > 8 or float(np.quantile(row, .95) - np.quantile(row, .05)) > 12:
            continue
        delta_a, delta_b = row - above, row - below
        median_a, median_b = float(np.median(delta_a)), float(np.median(delta_b))
        if not (3 <= abs(median_a) <= 90 and 3 <= abs(median_b) <= 90):
            continue
        if median_a * median_b <= 0:
            continue
        consistent = (np.abs(delta_a - median_a) <= 6) & (np.abs(delta_b - median_b) <= 6)
        if float(np.mean(consistent)) >= .88:
            return y
    return None


def _message_box(image: Image.Image, dpi: int, uia_box: Optional[Box]) -> Box:
    width, height = image.size
    if uia_box is not None:
        box = uia_box
        layout = "uia"
        divider = _find_input_divider(image, dpi)
        if divider is not None:
            boundary = divider - max(2, round(2 * dpi / 96))
            if box[1] + 120 <= boundary < box[3]:
                box = (box[0], box[1], box[2], boundary)
    else:
        divider = _find_input_divider(image, dpi)
        # The lower part is intentionally excluded when the client does not
        # expose its layout. Unknown pixels must not be accepted as a send.
        safe_bottom = min(round(height * .62), height - max(round(160 * dpi / 96), round(height * .30)))
        bottom = divider - max(2, round(2 * dpi / 96)) if divider is not None else safe_bottom
        box = (0, round(height * .12), width, bottom)
        layout = "divider" if divider is not None else "fallback"
    left, top, right, bottom = [int(value) for value in box]
    if not (0 <= left < right <= width and 0 <= top < bottom <= height
            and right - left >= 160 and bottom - top >= 120):
        raise CaptureUnavailableError("聊天记录区域过小或无法与输入框分离，请扩大微信窗口。")
    image.info["capture_layout"] = layout
    return left, top, right, bottom


def _print_client(hwnd: int, size: Tuple[int, int], user32: Any, gdi32: Any) -> Optional[Image.Image]:
    width, height = size
    device, memory, bitmap, previous = None, None, None, None
    try:
        device = user32.GetDC(hwnd)
        if not device:
            return None
        memory = gdi32.CreateCompatibleDC(device)
        if not memory:
            return None
        info = _BitmapInfo()
        info.header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0,
                                        width * height * 4, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(memory, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            return None
        previous = gdi32.SelectObject(memory, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            return None
        # Render client coordinates, not the decorated outer window.
        if not user32.PrintWindow(hwnd, memory, 0x00000001 | 0x00000002):
            return None
        image = Image.frombytes("RGB", size, ctypes.string_at(bits, width * height * 4), "raw", "BGRX")
        if max(high - low for low, high in ImageStat.Stat(image).extrema) < 5:
            return None
        return image
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if previous and memory:
            gdi32.SelectObject(memory, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        if device:
            user32.ReleaseDC(hwnd, device)


def capture_chat_frame(hwnd: int, prefer_print_window: bool = False) -> ChatFrame:
    """Capture only a validated, foreground window's message area.

    No desktop fallback, focus activation, key presses or clipboard writes are
    performed. UIA is optional and used only for container geometry.
    """
    user32, gdi32 = _load_win32()
    with _physical_coordinates(user32):
        geometry = _geometry(user32, int(hwnd))
        left, top, right, bottom = geometry.client
        uia_box = _uia_message_box(int(hwnd), geometry.client, geometry.dpi)
        image, backend = None, "screen"
        if prefer_print_window:
            image = _print_client(int(hwnd), (right - left, bottom - top), user32, gdi32)
            if image is not None:
                backend = "printwindow"
        if image is None:
            try:
                image = ImageGrab.grab(bbox=geometry.client, all_screens=True)
            except Exception as exc:
                raise CaptureUnavailableError("无法读取微信聊天画面，已暂停确认。") from exc
        if image.size != (right - left, bottom - top):
            raise CaptureUnavailableError("微信截图尺寸变化，已暂停确认。")
        image = image.convert("RGB")
        crop = _message_box(image, geometry.dpi, uia_box)
        if _geometry(user32, int(hwnd)) != geometry:
            raise CaptureUnavailableError("捕获期间微信窗口位置或缩放发生变化，已暂停确认。")
        layout = image.info["capture_layout"]
        signature = (int(hwnd), geometry.window, geometry.client, geometry.dpi, crop, layout)
        cropped = image.crop(crop)
        cropped.info["capture_layout"] = layout
        return ChatFrame(cropped, signature, backend)
