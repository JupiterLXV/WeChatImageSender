import unittest

from PIL import Image, ImageDraw, ImageOps

from sender.core import CompletionGate, Observation, State
from sender.qt_visual import image_state


def fixture(indicator=None, dpi=120, background=(30, 30, 31),
            source_size=(600, 400), size_factor=1.0, offset=(0, 0)):
    source = Image.new('RGB', source_size, '#bad7ea')
    draw = ImageDraw.Draw(source)
    w, h = source_size
    draw.rectangle((.05*w, .075*h, .95*w, .4*h), fill='#193850')
    draw.ellipse((.3*w, .475*h, .7*w, .95*h), fill='#ffa04d')
    draw.rectangle((.1*w, .55*h, .2*w, .85*h), fill='#783925')
    scale = dpi / 96
    thumbnail = source.copy()
    thumbnail.thumbnail((round(240 * scale * size_factor), round(240 * scale * size_factor)))
    frame = Image.new('RGB', (round(560 * scale), thumbnail.height + round(18 * scale)), background)
    right, top = round(frame.width - 70 * scale) + offset[0], round(8 * scale) + offset[1]
    left = right - thumbnail.width
    frame.paste(thumbnail, (left, top))
    center_x, center_y = left - 18 * scale, top + thumbnail.height / 2
    r = 8 * scale
    draw = ImageDraw.Draw(frame)
    bounds = (center_x - r, center_y - r, center_x + r, center_y + r)
    if indicator == 'ring':
        draw.arc(bounds, 20, 320, fill='#aaa', width=max(1, round(1.5 * scale)))
    elif indicator == 'red':
        draw.ellipse(bounds, fill='#ff4d4d')
    elif indicator == 'unknown':
        draw.rectangle(bounds, fill='#aaa')
    elif indicator == 'covered':
        draw.rectangle((center_x - 20 * scale, center_y - 20 * scale,
                        center_x + 20 * scale, center_y + 20 * scale), fill='#aaa')
    return frame, source


class VisualTests(unittest.TestCase):
    def state(self, indicator=None, dpi=120, background=(30, 30, 31)):
        frame, source = fixture(indicator, dpi, background)
        return image_state(frame, source, dpi, '4.1.15.53').state

    def test_observed_ring_and_clear_sequence(self):
        gate = CompletionGate()
        self.assertEqual(self.state('ring'), State.SENDING)
        self.assertEqual(self.state(), State.CLEAR)
        self.assertFalse(gate.feed(Observation('same-image', self.state('ring')), 0))
        self.assertFalse(gate.feed(Observation('same-image', self.state()), 1))
        self.assertTrue(gate.feed(Observation('same-image', self.state()), 3))

    def test_clear_thumbnail_alone_never_confirms(self):
        gate = CompletionGate()
        for t in (0, 1, 10, 90):
            self.assertFalse(gate.feed(Observation('image', self.state()), t))

    def test_failure_unknown_and_overlay_block_completion(self):
        self.assertEqual(self.state('red'), State.FAILED)
        self.assertEqual(self.state('unknown'), State.UNKNOWN)
        self.assertEqual(self.state('covered'), State.UNKNOWN)

    def test_dpi_and_dark_or_light_background(self):
        for dpi in (96, 120, 144, 168, 192):
            for bg in ((30, 30, 31), (245, 245, 245)):
                self.assertEqual(self.state('ring', dpi, bg), State.SENDING)
                self.assertEqual(self.state(None, dpi, bg), State.CLEAR)

    def test_wrong_image_build_or_scale_is_unknown(self):
        frame, source = fixture()
        self.assertEqual(image_state(frame, Image.new('RGB', source.size, 'red'), 120,
                                     '4.1.15.53').state, State.UNKNOWN)
        self.assertEqual(image_state(frame, source, 120, '4.1.15.54').state, State.UNKNOWN)
        self.assertEqual(image_state(frame, source, 110, '4.1.15.53').state, State.UNKNOWN)

    def test_clipped_or_shifted_image_is_unknown(self):
        frame, source = fixture()
        clipped = frame.crop((0, 0, frame.width, frame.height - 30))
        self.assertEqual(image_state(clipped, source, 120, '4.1.15.53').state, State.UNKNOWN)
        shifted = Image.new('RGB', frame.size, '#1e1e1f')
        shifted.paste(frame, (-100, 0))
        self.assertEqual(image_state(shifted, source, 120, '4.1.15.53').state, State.UNKNOWN)

    def test_reported_wide_image_geometry_preserves_status_checks(self):
        # Actual report: a 1500x722 source renders 307x148, offset -2,+1,
        # instead of the nominal 300x144. Use generated content, not user files.
        for indicator, state in [(None, State.CLEAR), ('ring', State.SENDING),
                                 ('red', State.FAILED), ('covered', State.UNKNOWN)]:
            frame, source = fixture(indicator, source_size=(1500, 722),
                                    size_factor=307/300, offset=(-2, 1))
            result = image_state(frame, source, 120, '4.1.15.53')
            self.assertEqual(result.state, state)
            self.assertTrue(result.matched)
            expected = (303, 11, 610, 159)
            self.assertTrue(all(abs(a-b) <= 1 for a, b in zip(result.thumbnail_bounds, expected)),
                            result.thumbnail_bounds)

    def test_calibrated_portrait_and_landscape_across_scales(self):
        for dpi in (96, 120, 144, 168, 192):
            for size in ((1500, 722), (722, 1500)):
                frame, source = fixture('ring', dpi, source_size=size,
                                        size_factor=1.025, offset=(-2, 1))
                self.assertEqual(image_state(frame, source, dpi, '4.1.15.53').state, State.SENDING)

    def test_calibration_does_not_accept_unrelated_image_or_large_layout_change(self):
        frame, source = fixture(source_size=(1500, 722), size_factor=307/300, offset=(-2, 1))
        self.assertEqual(image_state(frame, ImageOps.mirror(source), 120, '4.1.15.53').state,
                         State.UNKNOWN)
        for factor, offset in [(1.25, (0, 0)), (.7, (0, 0)), (1, (-80, 0)), (1, (0, 25))]:
            frame, source = fixture(source_size=(1500, 722), size_factor=factor, offset=offset)
            self.assertEqual(image_state(frame, source, 120, '4.1.15.53').state, State.UNKNOWN)

    def test_calibrated_clear_image_still_requires_completion_evidence(self):
        frame, source = fixture(source_size=(1500, 722), size_factor=307/300, offset=(-2, 1))
        result = image_state(frame, source, 120, '4.1.15.53')
        self.assertEqual(result.state, State.CLEAR)
        gate = CompletionGate()
        for now in (0, 5, 100):
            self.assertFalse(gate.feed(Observation('wide-image', result.state), now))


if __name__ == '__main__':
    unittest.main()
