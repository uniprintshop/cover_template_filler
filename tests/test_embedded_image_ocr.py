"""Embedded banners must reach OCR even when stored as large PDF images."""

import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import thesis_cover_watch as watcher


class EmbeddedImageOcrTests(unittest.TestCase):
    def test_banner_survives_model_prompt_trimming(self):
        page = SimpleNamespace(extract_text=lambda: "Cover fields and abstract " * 200)
        pdf = SimpleNamespace(pages=[page])
        with patch.object(watcher.pdfplumber, "open") as opened, \
                patch.object(watcher, "extract_page_image_text",
                             return_value="Westfalische Hochschule Gelsenkirchen"):
            opened.return_value.__enter__.return_value = pdf
            text = watcher.extract_digital(Path("cover.pdf"))
        self.assertIn("Westfalische Hochschule", watcher.cover_text_for_model(text))
        self.assertIn("Cover fields and abstract", watcher.cover_text_for_model(text))

    def _extract(self, size):
        image = Image.effect_noise(size, 50).convert("RGB")
        data = io.BytesIO()
        image.save(data, "PNG")
        reader = SimpleNamespace(pages=[SimpleNamespace(images=[
            SimpleNamespace(data=data.getvalue())
        ])])
        observed = []

        def ocr(image, **kwargs):
            observed.append(image.size)
            return "Westfalische Hochschule Gelsenkirchen"

        with patch.object(watcher, "PdfReader", return_value=reader), \
                patch.object(watcher.pytesseract, "image_to_string", side_effect=ocr):
            text = watcher.extract_page_image_text(Path("cover.pdf"), 2)
        return text, observed

    def test_page_sized_banner_is_resized_and_read_instead_of_skipped(self):
        text, sizes = self._extract((1241, 1754))
        self.assertIn("Westfalische Hochschule", text)
        self.assertEqual(len(sizes), 1)
        width, height = sizes[0]
        self.assertEqual(height, 1600)
        self.assertAlmostEqual(width / height, 1241 / 1754, places=3)

    def test_small_logo_keeps_its_original_resolution(self):
        text, sizes = self._extract((400, 100))
        self.assertIn("Westfalische Hochschule", text)
        self.assertEqual(sizes, [(400, 100)])
