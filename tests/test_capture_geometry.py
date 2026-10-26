"""Offline capture tests: no real HWND, UIA tree, GDI or screenshot is used."""
import contextlib
import ctypes
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from wechat_sender import capture_win32 as capture


class FakeWindows:
    def __init__(self):
        self.exists, self.visible, self.minimized = True, True, False
        self.foreground, self.dpi = 88, 120
        self.window, self.origin, self.size = (90, 40, 730, 880), (100, 70), (620, 800)
        self.rect_ok, self.client_ok, self.origin_ok = True, True, True
        self.IsWindow = lambda _: self.exists
        self.IsWindowVisible = lambda _: self.visible
        self.IsIconic = lambda _: self.minimized
        self.GetForegroundWindow = lambda: self.foreground
        self.GetDpiForWindow = lambda _: self.dpi

    def GetWindowRect(self, _, pointer):
        box = pointer._obj
        box.left, box.top, box.right, box.bottom = self.window
        return self.rect_ok

    def GetClientRect(self, _, pointer):
        box = pointer._obj
        box.left, box.top = 0, 0
        box.right, box.bottom = self.size
        return self.client_ok

    def ClientToScreen(self, _, pointer):
        pointer._obj.x += self.origin[0]
        pointer._obj.y += self.origin[1]
        return self.origin_ok


class TestValidatedFrame(unittest.TestCase):
    def setUp(self):
        self.user = FakeWindows()
        self.gdi = SimpleNamespace()
        self.picture = Image.new("RGB", self.user.size, "white")
        self.mock_win32 = patch.object(capture, "_load_win32", return_value=(self.user, self.gdi))
        self.mock_uia = patch.object(capture, "_uia_message_box", return_value=None)
        self.mock_grab = patch.object(capture.ImageGrab, "grab", return_value=self.picture)
        self.mock_win32.start()
        self.mock_uia.start()
        self.grab = self.mock_grab.start()
        self.addCleanup(self.mock_win32.stop)
        self.addCleanup(self.mock_uia.stop)
        self.addCleanup(self.mock_grab.stop)

    def test_capture_uses_physical_client_coordinates_and_excludes_lower_area(self):
        frame = capture.capture_chat_frame(88)
        self.grab.assert_called_once_with(bbox=(100, 70, 720, 870), all_screens=True)
        self.assertEqual(frame.image.size, (620, 400))
        self.assertEqual(frame.signature[:5], (88, self.user.window, (100, 70, 720, 870), 120, (0, 96, 620, 496)))
        self.assertEqual(frame.backend, "screen")
        self.assertEqual(frame.image.info["capture_layout"], "fallback")

    def test_invalid_window_visibility_or_foreground_never_grabs_desktop(self):
        for attribute, value in [("exists", False), ("visible", False), ("minimized", True), ("foreground", 99)]:
            with self.subTest(attribute=attribute):
                old = getattr(self.user, attribute)
                setattr(self.user, attribute, value)
                with self.assertRaises(capture.CaptureUnavailableError):
                    capture.capture_chat_frame(88)
                setattr(self.user, attribute, old)
        self.grab.assert_not_called()

    def test_failed_geometry_calls_do_not_produce_a_frame(self):
        for attribute in ("rect_ok", "client_ok", "origin_ok"):
            with self.subTest(attribute=attribute):
                setattr(self.user, attribute, False)
                with self.assertRaises(capture.CaptureUnavailableError):
                    capture.capture_chat_frame(88)
                setattr(self.user, attribute, True)
        self.grab.assert_not_called()

    def test_small_or_zero_size_window_is_rejected(self):
        self.user.size = (390, 800)
        with self.assertRaisesRegex(capture.CaptureUnavailableError, "过小"):
            capture.capture_chat_frame(88)
        self.grab.assert_not_called()

    def test_negative_monitor_coordinates_are_preserved(self):
        self.user.window = (-1710, 220, -1070, 1060)
        self.user.origin = (-1700, 250)
        frame = capture.capture_chat_frame(88)
        self.grab.assert_called_once_with(bbox=(-1700, 250, -1080, 1050), all_screens=True)
        self.assertEqual(frame.signature[2], (-1700, 250, -1080, 1050))

    def test_window_dpi_movement_or_focus_change_during_capture_is_unknown(self):
        for kind in ("move", "dpi", "focus"):
            with self.subTest(kind=kind):
                self.user = FakeWindows()
                with patch.object(capture, "_load_win32", return_value=(self.user, self.gdi)):
                    def changed(**_):
                        if kind == "move":
                            self.user.origin = (110, 70)
                        elif kind == "dpi":
                            self.user.dpi = 144
                        else:
                            self.user.foreground = 99
                        return self.picture
                    self.grab.side_effect = changed
                    with self.assertRaises(capture.CaptureUnavailableError):
                        capture.capture_chat_frame(88)

    def test_wrong_screenshot_size_is_not_stretched_to_fit(self):
        self.grab.return_value = Image.new("RGB", (621, 800))
        with self.assertRaisesRegex(capture.CaptureUnavailableError, "尺寸变化"):
            capture.capture_chat_frame(88)

    def test_grab_error_does_not_fall_back_to_a_desktop_capture(self):
        self.grab.side_effect = OSError("unavailable")
        with self.assertRaises(capture.CaptureUnavailableError):
            capture.capture_chat_frame(88)
        self.assertEqual(self.grab.call_count, 1)

    def test_print_and_screen_share_crop_coordinates_and_identify_backend(self):
        with patch.object(capture, "_uia_message_box", return_value=(150, 100, 620, 500)), patch.object(
            capture, "_print_client", return_value=self.picture
        ):
            printed = capture.capture_chat_frame(88, prefer_print_window=True)
            self.grab.assert_not_called()
            screen = capture.capture_chat_frame(88)
        self.assertEqual(printed.signature, screen.signature)
        self.assertEqual(printed.image.size, screen.image.size)
        self.assertEqual(printed.backend, "printwindow")
        self.assertEqual(screen.backend, "screen")
        self.assertEqual(screen.image.info["capture_layout"], "uia")

    def test_print_failure_has_an_explicit_screen_backend(self):
        with patch.object(capture, "_print_client", return_value=None):
            frame = capture.capture_chat_frame(88, prefer_print_window=True)
        self.assertEqual(frame.backend, "screen")
        self.grab.assert_called_once()

    def test_thread_dpi_context_is_restored_after_an_error(self):
        calls = []
        self.user.SetThreadDpiAwarenessContext = lambda value: calls.append(value) or 55
        self.user.visible = False
        with self.assertRaises(capture.CaptureUnavailableError):
            capture.capture_chat_frame(88)
        self.assertEqual(calls[0].value, ctypes.c_void_p(-4).value)
        self.assertEqual(calls[1], 55)


class TestMessageAreaGeometry(unittest.TestCase):
    @staticmethod
    def node(kind, box, children=()):
        return SimpleNamespace(ControlTypeName=kind, IsOffscreen=False,
                               BoundingRectangle=SimpleNamespace(left=box[0], top=box[1], right=box[2], bottom=box[3]),
                               GetChildren=lambda: list(children))

    def test_uia_messages_are_capped_above_the_editor_toolbar(self):
        edit = self.node("EditControl", (280, 670, 710, 855))
        listing = self.node("ListControl", (280, 160, 710, 690))
        listing.GetChildren = Mock(side_effect=AssertionError("message items are not needed"))
        root = self.node("WindowControl", (100, 70, 720, 870), [edit, listing])
        fake = SimpleNamespace(ControlFromHandle=lambda _: root, UIAutomationInitializerInThread=contextlib.nullcontext)
        with patch.dict(sys.modules, {"uiautomation": fake}):
            result = capture._uia_message_box(88, (100, 70, 720, 870), 120)
        self.assertEqual(result, (180, 90, 610, 555))
        listing.GetChildren.assert_not_called()

    def test_only_root_panes_do_not_establish_a_message_area(self):
        pane = self.node("PaneControl", (100, 70, 720, 870))
        root = self.node("WindowControl", (100, 70, 720, 870), [pane])
        with patch.dict(sys.modules, {"uiautomation": SimpleNamespace(ControlFromHandle=lambda _: root)}):
            self.assertIsNone(capture._uia_message_box(88, (100, 70, 720, 870), 120))

    def test_search_edit_does_not_define_the_composer_boundary(self):
        edit = self.node("EditControl", (110, 80, 300, 115))
        root = self.node("WindowControl", (100, 70, 720, 870), [edit])
        with patch.dict(sys.modules, {"uiautomation": SimpleNamespace(ControlFromHandle=lambda _: root)}):
            self.assertIsNone(capture._uia_message_box(88, (100, 70, 720, 870), 120))

    def test_short_text_line_below_an_attachment_is_not_a_full_composer(self):
        edit = self.node("EditControl", (280, 800, 710, 835))
        root = self.node("WindowControl", (100, 70, 720, 870), [edit])
        with patch.dict(sys.modules, {"uiautomation": SimpleNamespace(ControlFromHandle=lambda _: root)}):
            self.assertIsNone(capture._uia_message_box(88, (100, 70, 720, 870), 120))

    def test_light_dark_and_scaled_dividers_exclude_a_draft_thumbnail(self):
        for scale in (1, 1.25, 1.5, 2):
            for background, line in ((248, 210), (38, 60)):
                with self.subTest(scale=scale, background=background):
                    width, height = round(700 * scale), round(800 * scale)
                    image = Image.new("RGB", (width, height), (background,) * 3)
                    draw = ImageDraw.Draw(image)
                    y = round(560 * scale)
                    draw.line((0, y, width - 1, y), fill=(line,) * 3, width=max(1, round(scale)))
                    draw.rectangle((round(350 * scale), round(590 * scale), round(650 * scale), round(740 * scale)), fill="blue")
                    box = capture._message_box(image, round(96 * scale), None)
                    self.assertLess(box[3], y)
                    self.assertEqual(image.info["capture_layout"], "divider")
                    self.assertFalse(np.any(np.all(np.asarray(image.crop(box)) == (0, 0, 255), axis=2)))

    def test_expanded_editor_divider_excludes_a_higher_draft(self):
        image = Image.new("RGB", (700, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.line((0, 430, 699, 430), fill=(210, 210, 210))
        draw.rectangle((350, 450, 650, 700), fill="blue")
        box = capture._message_box(image, 96, None)
        self.assertLess(box[3], 430)
        self.assertFalse(np.any(np.all(np.asarray(image.crop(box)) == (0, 0, 255), axis=2)))

    def test_visible_divider_can_tighten_an_outdated_editor_boundary(self):
        image = Image.new("RGB", (700, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.line((0, 430, 699, 430), fill=(210, 210, 210))
        draw.rectangle((350, 450, 650, 700), fill="blue")
        box = capture._message_box(image, 96, (0, 100, 700, 600))
        self.assertLess(box[3], 430)
        self.assertFalse(np.any(np.all(np.asarray(image.crop(box)) == (0, 0, 255), axis=2)))

    def test_short_photo_edge_is_not_a_full_width_composer_divider(self):
        image = Image.new("RGB", (700, 800), "white")
        ImageDraw.Draw(image).rectangle((350, 450, 650, 580), fill=(80, 80, 80))
        self.assertIsNone(capture._find_input_divider(image, 96))

    def test_unknown_layout_is_marked_as_conservative_fallback(self):
        image = Image.new("RGB", (700, 800), "white")
        box = capture._message_box(image, 96, None)
        self.assertLessEqual(box[3], round(800 * .62))
        self.assertEqual(image.info["capture_layout"], "fallback")

    def test_inverted_or_too_small_message_bounds_are_rejected(self):
        for box in [(0, 200, 700, 190), (0, 100, 150, 500), (0, 100, 700, 150), (0, 100, 701, 500)]:
            with self.subTest(box=box), self.assertRaises(capture.CaptureUnavailableError):
                capture._message_box(Image.new("RGB", (700, 800)), 96, box)


class TestGdiCleanup(unittest.TestCase):
    def test_64_bit_pointer_types_are_declared_before_native_calls(self):
        user = SimpleNamespace(**{name: Mock() for name in [
            "IsWindow", "IsWindowVisible", "IsIconic", "GetForegroundWindow", "GetWindowRect", "GetClientRect",
            "ClientToScreen", "GetDpiForWindow", "SetThreadDpiAwarenessContext", "GetDC", "ReleaseDC", "PrintWindow",
        ]})
        gdi = SimpleNamespace(**{name: Mock() for name in [
            "CreateCompatibleDC", "CreateDIBSection", "SelectObject", "DeleteObject", "DeleteDC",
        ]})
        capture._load_win32.cache_clear()
        try:
            with patch.object(capture.sys, "platform", "win32"), patch.object(
                capture.ctypes, "WinDLL", side_effect=[user, gdi], create=True
            ):
                capture._load_win32()
            for function in (user.GetForegroundWindow, user.GetDC, gdi.CreateCompatibleDC,
                             gdi.CreateDIBSection, gdi.SelectObject):
                self.assertIs(function.restype, ctypes.c_void_p)
            self.assertEqual(user.PrintWindow.argtypes[0:2], [ctypes.c_void_p, ctypes.c_void_p])
        finally:
            capture._load_win32.cache_clear()

    def test_partial_allocation_failure_releases_existing_handles(self):
        user = SimpleNamespace(GetDC=Mock(return_value=123), ReleaseDC=Mock())
        gdi = SimpleNamespace(CreateCompatibleDC=Mock(return_value=456), CreateDIBSection=Mock(return_value=0),
                              DeleteDC=Mock(), DeleteObject=Mock(), SelectObject=Mock())
        self.assertIsNone(capture._print_client(88, (600, 800), user, gdi))
        gdi.DeleteDC.assert_called_once_with(456)
        user.ReleaseDC.assert_called_once_with(88, 123)
        gdi.DeleteObject.assert_not_called()
        self.assertEqual(ctypes.sizeof(capture._BitmapInfoHeader), 40)

    def test_print_failure_restores_selection_and_releases_every_handle(self):
        buffer = ctypes.create_string_buffer(600 * 800 * 4)
        def create(_dc, _info, _usage, bits, *_):
            bits._obj.value = ctypes.addressof(buffer)
            return 789
        user = SimpleNamespace(GetDC=Mock(return_value=123), ReleaseDC=Mock(), PrintWindow=Mock(return_value=False))
        gdi = SimpleNamespace(CreateCompatibleDC=Mock(return_value=456), CreateDIBSection=Mock(side_effect=create),
                              DeleteDC=Mock(), DeleteObject=Mock(), SelectObject=Mock(return_value=1001))
        self.assertIsNone(capture._print_client(88, (600, 800), user, gdi))
        self.assertEqual(gdi.SelectObject.call_args_list[-1].args, (456, 1001))
        gdi.DeleteObject.assert_called_once_with(789)
        gdi.DeleteDC.assert_called_once_with(456)
        user.ReleaseDC.assert_called_once_with(88, 123)
        user.PrintWindow.assert_called_once_with(88, 456, 3)
