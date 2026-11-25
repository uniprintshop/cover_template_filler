import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import thesis_cover_watch as w
from cover_fields import set_institution_notes


class ValidationTests(unittest.TestCase):
    def test_birth_year_cannot_bypass_filtered_candidates(self):
        cover = "Geboren am 01.01.1995 in Hagen\nAbgabe 2026"
        years = w.years_in(w.cover_matching_blob(cover))
        self.assertEqual(w.resolve_year(cover, "1995", years, "")[0], "2026")

    def test_abbreviated_birth_line_is_excluded(self):
        self.assertNotIn("Wuppertal", w.cover_matching_blob("Geb. 1995 in Wuppertal\n2026"))

    def test_city_word_extension_is_not_school_evidence(self):
        self.assertFalse(w._alias_in_blob("wuppertal", "wuppertaler institut"))
        self.assertTrue(w._alias_in_blob("wuppertal", "derbergischenuniversitatwuppertal"))

    def test_later_ocr_cluster_can_match(self):
        blob = "hochschule " + "x " * 250 + "westfalische gelsenkirchen hochschule"
        self.assertTrue(w._alias_in_blob("westfälische hochschule gelsenkirchen", blob, lenient=True))

    def test_deadline_does_not_shorten_settle_period(self):
        times = iter([0, 0, 59.8, 59.8, 60.1, 60.1])
        stat = SimpleNamespace(st_size=100, st_mtime_ns=1)
        with patch.object(w.time, "monotonic", side_effect=lambda: next(times)), \
                patch.object(w.time, "sleep"), patch.object(Path, "stat", return_value=stat):
            self.assertFalse(w.wait_until_stable(Path("cover.pdf"), 1.5))

    def test_upf_glyph_punctuation_survives_roundtrip(self):
        template = Path(__file__).resolve().parents[1] / "Prägungen/Westfälische_Hochschule_Gelsenkirchen.upf"
        fields = {"degree_text": "Masterarbeit", "author": "Audit { Example; Test}", "year": "2026"}
        filled = w.fill_upf(template.read_text(), fields, {})
        blocks = w.find_text_blocks(filled)
        self.assertEqual(len(blocks), 2)
        self.assertIn(fields["author"], blocks[1].joined())

    def test_institution_note_replacement_preserves_other_notes(self):
        fields = {"notes": ["Title omitted", "Old institution outcome"],
                  "institution_notes": ["Old institution outcome"]}
        set_institution_notes(fields, ["Recovered by OCR"])
        self.assertEqual(fields["notes"], ["Title omitted", "Recovered by OCR"])

    def test_long_unicode_names_leave_room_for_variant_suffixes(self):
        first = w.safe_stem("Ä" * 100)
        second = w.safe_stem("Ä" * 99 + "Ö")
        self.assertLessEqual(len(first.encode("utf-8")), 64)
        self.assertNotEqual(first, second)

    def test_all_repository_templates_roundtrip_author_and_year(self):
        root = Path(__file__).resolve().parents[1]
        cfg = w.load_config(root / "config.example.json")
        templates = list((root / "Prägungen").glob("*.upf"))
        self.assertGreater(len(templates), 200)
        for template in templates:
            with self.subTest(template=template.name), patch.object(w.LOG, "warning"):
                fields = {"degree_text": "Masterarbeit", "author": "Audit Example", "year": "2026", "title": "A title"}
                filled = w.fill_upf(template.read_text(), fields, cfg)
                decoded = " ".join(b.joined() for b in w.find_text_blocks(filled))
                self.assertIn("Audit Example", decoded)
                self.assertIn("2026", decoded)
