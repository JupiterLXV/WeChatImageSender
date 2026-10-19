import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from sender.core import SenderError, file_hash, scan_folder
from sender.images import UnsupportedFrames, load_rgb
from sender.qt_visual import prepare_source
from sender.winutil import copy_image


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def mpo(self, orientation=None):
        path = self.root / 'phone.JPG'
        primary = Image.new('RGB', (80, 50), '#ed2424')
        auxiliary = Image.new('RGB', (40, 25), '#164bea')
        kwargs = {}
        if orientation:
            exif = Image.Exif()
            exif[274] = orientation
            kwargs['exif'] = exif
        primary.save(path, 'MPO', save_all=True, append_images=[auxiliary], **kwargs)
        return path

    def test_jpg_mpo_scans_as_one_photo_without_changing_original(self):
        path = self.mpo()
        before = file_hash(path)
        with Image.open(path) as image:
            self.assertEqual(image.format, 'MPO')
            self.assertEqual(image.n_frames, 2)
        photos = scan_folder(self.root, False, True)
        self.assertEqual([p.name for p in photos], ['phone.JPG'])
        self.assertEqual(photos[0].sha256, before)
        self.assertEqual(file_hash(path), before)

    def test_mpo_always_reads_primary_not_auxiliary(self):
        image = load_rgb(self.mpo())
        self.assertEqual(image.size, (80, 50))
        r, g, b = image.getpixel((20, 20))
        self.assertGreater(r, 200)
        self.assertLess(b, 60)

    def test_mpo_orientation_matches_clipboard_pixels(self):
        path = self.mpo(orientation=6)
        expected = prepare_source(path)
        self.assertEqual(expected.size, (50, 80))
        with patch('sender.winutil.clipboard') as clipboard:
            copy_image(path)
        dib, format_id = clipboard.call_args.args
        self.assertEqual(format_id, 8)
        actual = Image.open(io.BytesIO(dib)).convert('RGB')
        self.assertEqual(actual.size, expected.size)
        self.assertIsNone(ImageChops.difference(actual, expected).getbbox())

    def test_animation_and_multiple_pages_still_rejected_even_named_jpg(self):
        for format_name in ('GIF', 'PNG', 'TIFF', 'WEBP'):
            with self.subTest(format=format_name):
                path = self.root / 'animation.JPG'
                Image.new('RGB', (40, 30), 'red').save(
                    path, format_name, save_all=True,
                    append_images=[Image.new('RGB', (40, 30), 'blue')])
                with self.assertRaisesRegex(SenderError, '动图或多页'):
                    scan_folder(self.root, False, True)
                with self.assertRaises(UnsupportedFrames):
                    load_rgb(path)

    def test_ordinary_jpeg_remains_supported(self):
        path = self.root / 'plain.jpg'
        Image.new('RGB', (80, 60), '#18b444').save(path)
        self.assertEqual(len(scan_folder(self.root, False, True)), 1)
        self.assertEqual(load_rgb(path).size, (80, 60))

    def test_alpha_background_is_identical_for_matching_and_clipboard(self):
        path = self.root / 'alpha.png'
        Image.new('RGBA', (50, 50), (255, 0, 0, 0)).save(path)
        with patch('sender.winutil.clipboard') as clipboard:
            copy_image(path)
        actual = Image.open(io.BytesIO(clipboard.call_args.args[0])).convert('RGB')
        expected = prepare_source(path)
        self.assertEqual(expected.getpixel((0, 0)), (255, 255, 255))
        self.assertIsNone(ImageChops.difference(actual, expected).getbbox())
