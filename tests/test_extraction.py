import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import cover_extract as e
import thesis_cover_watch as w


class ExtractionTests(unittest.TestCase):
    def image(self):
        stream = io.BytesIO()
        Image.new("RGB", (200, 50), "white").save(stream, "PNG")
        return SimpleNamespace(data=stream.getvalue())

    def test_small_compressed_logo_is_read_once_across_pages(self):
        image = self.image()
        self.assertLess(len(image.data), 2000)
        reader = SimpleNamespace(pages=[SimpleNamespace(images=[image]), SimpleNamespace(images=[image])])
        with patch.object(e, "_ocr", return_value="Example University") as ocr:
            blocks = e._image_blocks(Path("cover.pdf"), 2, {}, reader)
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].page, 1)

    def test_image_count_and_timeout_are_bounded(self):
        first, second = self.image(), SimpleNamespace(data=b"invalid but different image")
        reader = SimpleNamespace(pages=[SimpleNamespace(images=[first, second])])
        with patch.object(e, "_ocr", return_value="Example University") as ocr:
            e._image_blocks(Path("cover.pdf"), 2, {"ocr_image_limit": 1, "ocr_timeout_seconds": 2}, reader)
        self.assertEqual(ocr.call_count, 1)
        self.assertLessEqual(ocr.call_args.args[2], 2)

    def test_prompt_reserves_space_for_digital_fields(self):
        evidence = e.CoverEvidence([
            e.EvidenceBlock(1, "embedded_ocr", "Logo text " * 500),
            e.EvidenceBlock(1, "digital", "Vorgelegt von: Audit Example\n2026\n" + "Abstract " * 500),
        ], "digital+images")
        prompt = evidence.prompt()
        self.assertLessEqual(len(prompt), 2400)
        self.assertIn("Audit Example", prompt)
        self.assertIn("2026", prompt)

    def test_lenient_matching_is_limited_to_ocr_blocks(self):
        universities = [{"id": "example", "aliases": ["alpha institute beta"], "template": "XX.upf"}]
        text = "alpha beta institute"
        for source, accepted in (("digital", False), ("embedded_ocr", True)):
            with self.subTest(source=source):
                evidence = e.CoverEvidence([e.EvidenceBlock(1, source, text)], source)
                _, risks, _ = w.resolve_university(text, "example", universities, None, evidence=evidence)
                self.assertEqual(not risks, accepted)

    def test_institution_words_cannot_join_across_pages(self):
        evidence = e.CoverEvidence([e.EvidenceBlock(1, "ocr", "alpha institute"),
                                    e.EvidenceBlock(2, "ocr", "beta")], "ocr")
        uni = [{"id": "example", "aliases": ["alpha institute beta"], "template": "XX.upf"}]
        self.assertIn("university_not_on_cover", w.resolve_university(
            evidence.text, "example", uni, None, evidence=evidence)[1])
