"""Ensure the standalone export preserves the real OCR confirmation path."""

import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.export_single_file import export


class TestStandaloneExport(unittest.TestCase):
    def test_exported_ocr_alias_supports_title_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = export(Path(directory))
            namespace = runpy.run_path(str(output), run_name='standalone_export_test')

        self.assertIs(namespace['recognize_local_text'], namespace['recognize'])
        adapter = object.__new__(namespace['WindowsWeChatAdapter'])
        sample = Image.new('RGB', (320, 100), 'white')
        adapter._capture_ocr_area = lambda area: sample
        lines = [namespace['OcrLine']('文件传输助手', (12, 8, 160, 24))]
        with patch.dict(adapter._ocr_chat_title.__globals__, {'recognize_local_text': lambda image: lines}):
            self.assertEqual(adapter._ocr_chat_title(), '文件传输助手')


if __name__ == '__main__':
    unittest.main()
