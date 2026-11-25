import unittest
from pathlib import Path
from unittest.mock import patch

import thesis_cover_watch as watch
from cover_extract import CoverEvidence, EvidenceBlock
from cover_fields import can_stamp
from reliability_suite import assess


class InstitutionRoleTests(unittest.TestCase):
    def test_ocr_cannot_publish_an_explicitly_ambiguous_answer(self):
        fields = {"risks": ["no_university_on_cover"], "university_id": "",
                  "author": "Nora Seidel", "year": "2026", "notes": []}
        evidence = CoverEvidence([EvidenceBlock(1, "ocr", "Two universities award this thesis. " * 5)], "ocr")
        answer = {"university": "test", "institution_status": "ambiguous"}
        cfg = {"universities": [{"id": "test", "template": "Test.upf"}]}
        with patch.object(watch, "extract_ocr", return_value=evidence), patch.object(watch, "_model_fields", return_value=answer):
            result, used = watch._rescue_university_from_ocr(Path("unused.pdf"), {}, cfg, fields)
        self.assertTrue(used)
        self.assertFalse(result["ok_to_stamp"])
        self.assertIn("university_ambiguous", result["risks"])
        self.assertEqual(assess({"decision": "reject"}, result, [], error_kind="rejection")["verdict"], "correct_rejection")

    def test_joint_awarding_is_vetoed_independently_of_model(self):
        self.assertTrue(watch.joint_award_on_cover("Gemeinsame Abschlussarbeit beider Hochschulen"))
        self.assertTrue(watch.joint_award_on_cover("Jointly awarded by both institutions"))
        self.assertFalse(watch.joint_award_on_cover("Gemeinsame Betreuung durch zwei Professoren"))
        self.assertFalse(watch.joint_award_on_cover("Joint thesis by two students"))

    def test_city_cannot_override_different_explicit_school(self):
        schools = [
            {"id": "college", "template": "Dortmund_Hochschule.upf", "aliases": ["Hochschule Dortmund"]},
            {"id": "university", "template": "TU_Dortmund.upf", "aliases": ["Technische Universität Dortmund"]},
        ]
        _, risks, _ = watch.resolve_university("Technische Universität Dortmund", "college", schools, None)
        self.assertIn("university_not_on_cover", risks)
        _, risks, _ = watch.resolve_university("Hochschule Dortmund", "college", schools, None)
        self.assertEqual(risks, [])

    def test_ambiguous_pick_is_blocked_even_when_present(self):
        cfg = {"universities": [{"id": "test", "name": "Test University", "aliases": ["Test University"]}]}
        answer = {"university": "test", "institution_status": "ambiguous", "author": "Nora Seidel", "year": "2026"}
        with patch.object(watch, "_model_fields", return_value=answer):
            fields = watch.extract_fields("Test University\nNora Seidel\n2026", {}, cfg)
        self.assertIn("university_ambiguous", fields["risks"])
        self.assertEqual(fields["university_id"], "")
        self.assertFalse(can_stamp(fields))
        self.assertFalse(watch._institution_unresolved(fields["risks"]))

    def test_birthplace_is_removed_only_with_combined_label(self):
        text = "Autorennamen, Geburtsort // Name, Place of Birth\nNora Seidel, Hagen\n2026"
        author, _ = watch.resolve_author(text, "Nora Seidel, Hagen", [])
        self.assertEqual(author, "Nora Seidel")
        author, _ = watch.resolve_author("Öztürk, Cem\n2026", "Öztürk, Cem", [])
        self.assertEqual(author, "Öztürk, Cem")

    def test_prompt_keeps_page_two_footer_and_image_positions(self):
        evidence = CoverEvidence([
            EvidenceBlock(1, "embedded_ocr", "First University", "upper image"),
            EvidenceBlock(1, "embedded_ocr", "Partner College", "lower image"),
            EvidenceBlock(1, "digital", "Nora Seidel\n2026"),
            EvidenceBlock(2, "digital", "Abstract " * 1500 + "\nAwarding University"),
        ], "digital+images")
        prompt = evidence.prompt()
        self.assertLessEqual(len(prompt), 2400)
        self.assertIn("upper image", prompt)
        self.assertIn("lower image", prompt)
        self.assertIn("page 2", prompt)
        self.assertIn("Awarding University", prompt)
        self.assertIn("Nora Seidel", prompt)


if __name__ == "__main__":
    unittest.main()
