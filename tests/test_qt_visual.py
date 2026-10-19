import unittest

from PIL import Image, ImageDraw

from sender.core import CompletionGate, Observation, State
from sender.qt_visual import image_state


def fixture(indicator=None, dpi=120, background=(30, 30, 31)):
    source = Image.new('RGB', (600, 400), '#bad7ea')
    draw = ImageDraw.Draw(source)
    draw.rectangle((30, 30, 570, 160), fill='#193850')
    draw.ellipse((180, 190, 420, 380), fill='#ffa04d')
    scale = dpi / 96
    thumbnail = source.copy()
    thumbnail.thumbnail((round(240 * scale), round(240 * scale)))
    frame = Image.new('RGB', (round(560 * scale), thumbnail.height + round(18 * scale)), background)
    right, top = round(frame.width - 70 * scale), round(8 * scale)
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


if __name__ == '__main__':
    unittest.main()
