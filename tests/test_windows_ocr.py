"""Offline tests for the optional Windows OCR boundary (no screen capture)."""

import asyncio
import threading
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from wechat_sender import windows_ocr


def _word(x, y, width, height):
    return SimpleNamespace(bounding_rect=SimpleNamespace(x=x, y=y, width=width, height=height))


class FakeBitmap:
    created = []

    def __init__(self, payload, width, height):
        self.payload, self.width, self.height = payload, width, height
        self.closed = False

    @classmethod
    def create_copy_with_alpha_from_buffer(cls, payload, pixel_format, width, height, alpha_mode):
        bitmap = cls(payload, width, height)
        cls.created.append(bitmap)
        return bitmap

    def close(self):
        self.closed = True


class FakeWriter:
    def write_bytes(self, payload):
        self.payload = payload

    def detach_buffer(self):
        return self.payload

    def close(self):
        pass


class FakeEngine:
    def __init__(self, lines=(), fail=False):
        self.lines, self.fail = lines, fail

    async def recognize_async(self, bitmap):
        if self.fail:
            raise OSError("local OCR unavailable")
        return SimpleNamespace(lines=self.lines)


def _backend(engine, max_dimension=100):
    return windows_ocr._Backend(engine, max_dimension, FakeBitmap, "BGRA8", "IGNORE", FakeWriter)


class TestWindowsOcr(unittest.TestCase):
    def setUp(self):
        FakeBitmap.created.clear()

    def test_normalization_ignores_spacing_width_and_case_but_keeps_punctuation(self):
        self.assertEqual(windows_ocr.normalized_text(" 文\u3000件\t传\n输助手 ＡＢＣ１２ "), "文件传输助手abc12")
        self.assertNotEqual(windows_ocr.normalized_text("张三-测试"), windows_ocr.normalized_text("张三测试"))
        self.assertNotEqual(windows_ocr.normalized_text("file(1)"), windows_ocr.normalized_text("file1"))

    def test_line_bounds_union_words_and_return_original_coordinates(self):
        line = SimpleNamespace(text="文件 传输", words=[_word(10, 4, 12, 7), _word(26, 5, 15, 8)])
        with patch.object(windows_ocr, "_get_backend", return_value=_backend(FakeEngine([line]))):
            actual = windows_ocr.recognize(Image.new("RGB", (300, 150), "white"))
        self.assertEqual(actual, [windows_ocr.OcrLine("文件 传输", (30, 12, 93, 27))])
        self.assertEqual((FakeBitmap.created[0].width, FakeBitmap.created[0].height), (100, 50))
        self.assertTrue(FakeBitmap.created[0].closed)
        with self.assertRaises(FrozenInstanceError):
            actual[0].text = "changed"

    def test_clips_bounds_to_input_and_skips_lines_without_geometry(self):
        lines = [SimpleNamespace(text="one", words=[_word(-4, -3, 110, 90)]),
                 SimpleNamespace(text="unknown", words=[])]
        with patch.object(windows_ocr, "_get_backend", return_value=_backend(FakeEngine(lines))):
            self.assertEqual(windows_ocr.recognize(Image.new("RGB", (80, 60))),
                             [windows_ocr.OcrLine("one", (0, 0, 80, 60))])

    def test_supplies_bgra_bytes_with_transparency_composited_on_white(self):
        with patch.object(windows_ocr, "_get_backend", return_value=_backend(FakeEngine())):
            self.assertEqual(windows_ocr.recognize(Image.new("RGBA", (1, 1), (255, 0, 0, 255))), [])
            self.assertEqual(FakeBitmap.created[-1].payload, bytes([0, 0, 255, 255]))
            windows_ocr.recognize(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
            self.assertEqual(FakeBitmap.created[-1].payload, bytes([255, 255, 255, 255]))

    def test_missing_engine_and_runtime_failure_return_none_not_empty_success(self):
        with patch.object(windows_ocr, "_get_backend", return_value=None):
            self.assertFalse(windows_ocr.available())
            self.assertIsNone(windows_ocr.recognize(Image.new("RGB", (10, 10))))
        with patch.object(windows_ocr, "_get_backend", return_value=_backend(FakeEngine(fail=True))):
            self.assertIsNone(windows_ocr.recognize(Image.new("RGB", (10, 10))))
            self.assertTrue(FakeBitmap.created[-1].closed)

    def test_sync_api_does_not_try_nesting_asyncio_event_loop(self):
        async def misuse():
            return windows_ocr.recognize(Image.new("RGB", (10, 10)))
        with patch.object(windows_ocr, "_get_backend", return_value=_backend(FakeEngine())):
            self.assertIsNone(asyncio.run(misuse()))
        self.assertEqual(FakeBitmap.created, [])

    def test_engine_cache_is_thread_local(self):
        backends = []
        def create():
            backend = _backend(FakeEngine())
            backends.append(backend)
            return backend
        def worker():
            self.assertIs(windows_ocr._get_backend(), windows_ocr._get_backend())
        with patch.object(windows_ocr, "_load_backend", side_effect=create):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(backends), 2)
        self.assertIsNot(backends[0], backends[1])


if __name__ == "__main__":
    unittest.main()
