"""Optional, local Windows OCR for screenshots supplied by the caller.

No screen capture, network access, or input automation is performed here.  Call
``recognize`` from a worker: it synchronously waits for WinRT with asyncio.run.
PyWinRT and installed OCR language packs are optional; unavailable recognition
returns None, while a successful scan without text returns an empty list.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sys
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from PIL import Image


@dataclass(frozen=True)
class OcrLine:
    """Recognized text and its (x, y, width, height) in original image pixels."""

    text: str
    box: Tuple[int, int, int, int]


@dataclass(frozen=True)
class _Backend:
    engine: Any
    max_dimension: int
    bitmap_type: Any
    pixel_format: Any
    alpha_mode: Any
    writer_type: Any


_thread_state = threading.local()


def normalized_text(text: str) -> str:
    """Normalize width/case and Unicode whitespace, preserving punctuation."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized if not char.isspace())


def _load_backend() -> Optional[_Backend]:
    if sys.platform != "win32":
        return None
    try:
        from winrt.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        languages = list(OcrEngine.available_recognizer_languages)
        chinese = [lang for lang in languages if lang.language_tag.lower().startswith("zh")]
        chinese.sort(key=lambda lang: (
            "hans" not in lang.language_tag.lower() and lang.language_tag.lower() != "zh-cn",
            lang.language_tag,
        ))
        engine = None
        for language in chinese:
            with contextlib.suppress(Exception):
                engine = OcrEngine.try_create_from_language(language)
            if engine is not None:
                break
        if engine is None:
            with contextlib.suppress(Exception):
                engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            for language in languages:
                if language.language_tag.lower().startswith("en"):
                    with contextlib.suppress(Exception):
                        engine = OcrEngine.try_create_from_language(language)
                    if engine is not None:
                        break
        if engine is None:
            return None
        max_dimension = int(OcrEngine.max_image_dimension)
        if max_dimension < 1:
            return None
        return _Backend(engine, max_dimension, SoftwareBitmap,
                        BitmapPixelFormat.BGRA8, BitmapAlphaMode.IGNORE, DataWriter)
    except Exception:
        # Import/DLL errors and missing Windows language packs must not prevent
        # the application from opening or its UIA adapter from operating.
        return None


def _get_backend() -> Optional[_Backend]:
    if not hasattr(_thread_state, "backend"):
        _thread_state.backend = _load_backend()
    return _thread_state.backend


def available() -> bool:
    """Whether an installed local engine can be created on this thread."""
    return _get_backend() is not None


async def _recognize_async(engine: Any, bitmap: Any) -> Any:
    # WinRT IAsyncOperation is awaitable, but not a coroutine accepted directly
    # by asyncio.run, hence this small coroutine wrapper.
    return await engine.recognize_async(bitmap)


def recognize(image: Image.Image) -> Optional[List[OcrLine]]:
    """Recognize an in-memory PIL image; call from a background worker.

    Engines are created lazily per thread and never shared across callers.
    Oversized images are reduced to the Windows engine limit. Word bounds are
    unioned per line and mapped back to the caller's original image dimensions.
    Any missing capability or recognition failure returns None, never success.
    """
    bitmap = None
    writer = None
    try:
        backend = _get_backend()
        if backend is None or image.width < 1 or image.height < 1:
            return None
        # Do not create a coroutine that cannot be awaited when misused from an
        # existing asyncio loop; this synchronous API belongs in a worker.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return None

        original_width, original_height = image.size
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            prepared = Image.new("RGB", image.size, "white")
            prepared.paste(rgba, mask=rgba.getchannel("A"))
        else:
            prepared = image.convert("RGB")
        if max(prepared.size) > backend.max_dimension:
            ratio = backend.max_dimension / max(prepared.size)
            prepared = prepared.resize(
                (max(1, int(prepared.width * ratio)), max(1, int(prepared.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        width, height = prepared.size
        writer = backend.writer_type()
        writer.write_bytes(prepared.convert("RGBA").tobytes("raw", "BGRA"))
        bitmap = backend.bitmap_type.create_copy_with_alpha_from_buffer(
            writer.detach_buffer(), backend.pixel_format, width, height, backend.alpha_mode,
        )
        result = asyncio.run(_recognize_async(backend.engine, bitmap))
        scale_x, scale_y = original_width / width, original_height / height
        lines: List[OcrLine] = []
        for line in result.lines:
            rects = [word.bounding_rect for word in line.words]
            if not rects or not str(line.text).strip():
                continue
            left = max(0, min(original_width, math.floor(min(r.x for r in rects) * scale_x)))
            top = max(0, min(original_height, math.floor(min(r.y for r in rects) * scale_y)))
            right = max(left, min(original_width, math.ceil(max(r.x + r.width for r in rects) * scale_x)))
            bottom = max(top, min(original_height, math.ceil(max(r.y + r.height for r in rects) * scale_y)))
            if right > left and bottom > top:
                lines.append(OcrLine(str(line.text), (left, top, right - left, bottom - top)))
        return lines
    except Exception:
        return None
    finally:
        if bitmap is not None:
            with contextlib.suppress(Exception):
                bitmap.close()
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
