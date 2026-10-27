"""Offline reference-crop tests; no real window, UIA, GDI or desktop capture."""

import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from tests.test_capture_geometry import FakeWindows
from wechat_sender import capture_win32 as capture


class TestReferenceCapture(unittest.TestCase):
    def setUp(self):
        self.user = FakeWindows()
        self.gdi = SimpleNamespace()
        loader = patch.object(capture, "_load_win32", return_value=(self.user, self.gdi))
        loader.start()
        self.addCleanup(loader.stop)
        self.grab = Mock(side_effect=lambda *, bbox, all_screens:
                         Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "white"))
        grabber = patch.object(capture.ImageGrab, "grab", self.grab)
        grabber.start()
        self.addCleanup(grabber.stop)
        self.layout_probes = []
        for name in ("_uia_message_box", "_find_input_divider", "_message_box"):
            probe = patch.object(capture, name, side_effect=AssertionError("reference crop has no layout probe"))
            self.layout_probes.append(probe.start())
            self.addCleanup(probe.stop)

    def borderless(self):
        self.user.window = (100, 200, 1100, 1000)
        self.user.origin, self.user.size = (100, 200), (1000, 800)

    def test_fixed_outer_window_crop_matches_reference_rounding(self):
        frame = capture.capture_reference_frame(88)
        self.grab.assert_called_once_with(bbox=(269, 166, 724, 746), all_screens=True)
        self.assertEqual(frame.image.size, (455, 580))
        self.assertEqual(frame.image.info["capture_layout"], "reference")
        self.assertEqual(frame.backend, "screen")
        self.assertEqual(frame.signature, (88, (90, 40, 730, 880), (100, 70, 720, 870),
                                           120, (269, 166, 724, 746), "reference"))
        for probe in self.layout_probes:
            probe.assert_not_called()

    def test_physical_scaling_and_negative_monitor_coordinates_are_preserved(self):
        cases = [
            (96, (1000, 800), (-920, 220, -206, 772)),
            (120, (1250, 1000), (-850, 250, 44, 940)),
            (144, (1500, 1200), (-780, 280, 294, 1108)),
            (192, (2000, 1600), (-640, 340, 794, 1444)),
        ]
        for dpi, (width, height), expected in cases:
            with self.subTest(dpi=dpi):
                self.user.window = (-1200, 100, -1200 + width, 100 + height)
                self.user.origin, self.user.size, self.user.dpi = (-1200, 100), (width, height), dpi
                self.grab.reset_mock()
                frame = capture.capture_reference_frame(88)
                self.grab.assert_called_once_with(bbox=expected, all_screens=True)
                self.assertEqual(frame.signature[3], dpi)
                self.assertEqual(frame.image.size, (expected[2] - expected[0], expected[3] - expected[1]))

    def test_print_client_crop_uses_client_screen_origin(self):
        self.borderless()
        printed = Image.new("RGB", self.user.size, "white")
        ImageDraw.Draw(printed).rectangle((300, 140, 310, 150), fill="red")
        with patch.object(capture, "_print_client", return_value=printed) as print_client:
            result = capture.capture_reference_frame(88, prefer_print_window=True)
        print_client.assert_called_once_with(88, (1000, 800), self.user, self.gdi)
        self.grab.assert_not_called()
        self.assertEqual(result.backend, "printwindow")
        self.assertEqual(result.image.size, (714, 552))
        self.assertEqual(result.image.getpixel((20, 20)), (255, 0, 0))
        screen = capture.capture_reference_frame(88)
        self.assertEqual(result.signature, screen.signature)

    def test_unavailable_wrong_size_or_failing_print_uses_bounded_screen_crop(self):
        self.borderless()
        for output in (None, Image.new("RGB", (999, 800)), RuntimeError("synthetic print failure")):
            with self.subTest(output=type(output).__name__):
                self.grab.reset_mock()
                kwargs = {"side_effect": output} if isinstance(output, Exception) else {"return_value": output}
                with patch.object(capture, "_print_client", **kwargs):
                    frame = capture.capture_reference_frame(88, prefer_print_window=True)
                self.assertEqual(frame.backend, "screen")
                self.grab.assert_called_once_with(bbox=(380, 320, 1094, 872), all_screens=True)

    def test_reference_area_outside_client_cannot_be_padded_by_print_window(self):
        # The reference extends four pixels beyond this client's right edge.
        with patch.object(capture, "_print_client", return_value=Image.new("RGB", self.user.size)):
            result = capture.capture_reference_frame(88, prefer_print_window=True)
        self.assertEqual(result.backend, "screen")
        self.grab.assert_called_once_with(bbox=(269, 166, 724, 746), all_screens=True)

    def test_missing_or_unusable_window_never_requests_a_screenshot(self):
        with self.assertRaises(capture.CaptureUnavailableError):
            capture.capture_reference_frame(0)
        for attribute, value in (("exists", False), ("visible", False), ("minimized", True), ("foreground", 99)):
            with self.subTest(attribute=attribute):
                old = getattr(self.user, attribute)
                setattr(self.user, attribute, value)
                with self.assertRaises(capture.CaptureUnavailableError):
                    capture.capture_reference_frame(88)
                setattr(self.user, attribute, old)
        self.grab.assert_not_called()

    def test_geometry_failure_does_not_request_a_screenshot(self):
        for attribute in ("rect_ok", "client_ok", "origin_ok"):
            with self.subTest(attribute=attribute):
                setattr(self.user, attribute, False)
                with self.assertRaises(capture.CaptureUnavailableError):
                    capture.capture_reference_frame(88)
                setattr(self.user, attribute, True)
        self.grab.assert_not_called()

    def test_movement_dpi_or_focus_change_during_capture_is_rejected(self):
        for attribute, changed in (("window", (100, 40, 740, 880)), ("dpi", 144), ("foreground", 99)):
            with self.subTest(attribute=attribute):
                self.user = FakeWindows()
                def grab(**kwargs):
                    setattr(self.user, attribute, changed)
                    box = kwargs["bbox"]
                    return Image.new("RGB", (box[2] - box[0], box[3] - box[1]))
                with patch.object(capture, "_load_win32", return_value=(self.user, self.gdi)), patch.object(
                    capture.ImageGrab, "grab", side_effect=grab
                ):
                    with self.assertRaises(capture.CaptureUnavailableError):
                        capture.capture_reference_frame(88)

    def test_wrong_screen_size_is_not_resized_into_a_confirmation(self):
        self.grab.side_effect = None
        self.grab.return_value = Image.new("RGB", (454, 580))
        with self.assertRaisesRegex(capture.CaptureUnavailableError, "尺寸变化"):
            capture.capture_reference_frame(88)

    def test_screen_failure_has_no_whole_desktop_fallback(self):
        self.grab.side_effect = OSError("synthetic screen unavailable")
        with self.assertRaises(capture.CaptureUnavailableError):
            capture.capture_reference_frame(88)
        self.grab.assert_called_once_with(bbox=(269, 166, 724, 746), all_screens=True)

    def test_thread_dpi_context_is_restored_after_capture_error(self):
        calls = []
        self.user.SetThreadDpiAwarenessContext = lambda value: calls.append(value) or 55
        self.user.visible = False
        with self.assertRaises(capture.CaptureUnavailableError):
            capture.capture_reference_frame(88)
        self.assertEqual(calls[0].value, ctypes.c_void_p(-4).value)
        self.assertEqual(calls[1], 55)


if __name__ == "__main__":
    unittest.main()
