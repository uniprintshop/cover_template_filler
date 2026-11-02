"""Selection regressions; run with python -m unittest discover -s tests.

The picker is model-primary: the model names the awarding institution by its
config id, a loose cover-presence check vetoes (but never guesses) that pick,
and the stamped foil is read straight from config. These tests pin that
behaviour without calling any real model - ``_model_fields`` is patched to
script what the model "says".
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import thesis_cover_watch as watcher


class TemplateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = json.loads(
            (Path(__file__).resolve().parents[1] / "config.example.json").read_text()
        )
        self.universities = self.config["universities"]
        for university in self.universities:
            for name in (
                university.get("templates")
                or ([university.get("template")] if university.get("template") else [])
            ):
                if name:
                    (self.directory / name).touch()
        self.config["templates_dir"] = str(self.directory)
        # No live model in tests: the identifier is scripted per-test via
        # _model_fields, and the "model down" case uses this backend.
        self.config["model"] = {"backend": "none"}

    def _fields_with_model(self, cover: str, model_fields: dict) -> dict:
        with patch.object(watcher, "_model_fields", return_value=model_fields):
            return watcher.extract_fields(cover, {}, self.config)

    def test_model_pick_grounded_on_cover_is_stamped(self):
        cover = (
            "Bergische Universität Wuppertal\n"
            "Bachelorarbeit\n"
            "\n"
            "Vorgelegt von: Lena Hoffmann\n"
            "Wuppertal, 2026\n"
        )
        fields = self._fields_with_model(cover, {"university": "wuppertal"})
        self.assertEqual(fields["university_id"], "wuppertal")
        self.assertTrue(fields["ok_to_stamp"])
        self.assertNotIn("university_not_on_cover", fields["risks"])
        self.assertNotIn("no_university_on_cover", fields["risks"])

    def test_ungrounded_pick_is_not_stamped(self):
        # The model says Wuppertal, but no part of that foil's filename shows
        # on this cover, so the pick is vetoed rather than stamped.
        cover = (
            "Fakultät für Ingenieurwissenschaften\n"
            "Bachelorarbeit\n"
            "\n"
            "Vorgelegt von: Lena Hoffmann\n"
            "2026\n"
        )
        fields = self._fields_with_model(cover, {"university": "wuppertal"})
        self.assertIn("university_not_on_cover", fields["risks"])
        self.assertFalse(fields["ok_to_stamp"])
        # The pick is still recorded so an operator can see what was rejected.
        self.assertEqual(fields["university_id"], "wuppertal")

    def test_unknown_id_is_not_stamped(self):
        cover = (
            "Bergische Universität Wuppertal\n"
            "Bachelorarbeit\n"
            "\n"
            "Vorgelegt von: Lena Hoffmann\n"
            "2026\n"
        )
        fields = self._fields_with_model(cover, {"university": "does-not-exist"})
        self.assertIn("university_id_not_in_catalog:does-not-exist", fields["risks"])
        self.assertEqual(fields["university_id"], "")
        self.assertFalse(fields["ok_to_stamp"])

    def test_no_model_means_no_university(self):
        # The cover plainly says Wuppertal, but with the model down there is no
        # identifier, so nothing is stamped - a misread must never become a foil.
        cover = (
            "Bergische Universität Wuppertal\n"
            "Bachelorarbeit\n"
            "\n"
            "Vorgelegt von: Lena Hoffmann\n"
            "2026\n"
        )
        fields = watcher.extract_fields(cover, {}, self.config)
        self.assertIn("no_university_on_cover", fields["risks"])
        self.assertEqual(fields["university_id"], "")
        self.assertFalse(fields["ok_to_stamp"])

    def test_aachen_is_not_mistaken_for_birthplace(self):
        # "Wuppertal" appears only as a birthplace; the awarding institution on
        # the letterhead is Aachen. The stripped cover must not ground Wuppertal.
        cover = (
            "Rheinisch-Westfälische Technische Hochschule Aachen\n"
            "Masterarbeit\n"
            "\n"
            "Geboren am 12.03.1995 in Wuppertal\n"
            "Vorgelegt von: Lena Hoffmann\n"
            "2026\n"
        )
        fields = self._fields_with_model(cover, {"university": "rwth-aachen"})
        self.assertEqual(fields["university_id"], "rwth-aachen")
        self.assertNotEqual(fields["university_id"], "wuppertal")
        self.assertNotIn("university_not_on_cover", fields["risks"])
        self.assertTrue(fields["ok_to_stamp"])

    def test_a_neighbors_foil_does_not_join_this_school(self):
        # Membership is config-driven: offering a *neighbour's* foil as the
        # model's pick must never pull that neighbour's file into a school's
        # approved set.
        n = len(self.universities)
        for i, university in enumerate(self.universities):
            neighbour = self.universities[(i + 1) % n]
            needle = (
                neighbour.get("templates") or [neighbour.get("template")]
            )[0]
            own = {
                x
                for x in (
                    university.get("templates")
                    or ([university.get("template")] if university.get("template") else [])
                )
                if x
            }
            variants = watcher.template_variants_for(
                university, needle, self.directory
            )
            with self.subTest(university=university["id"]):
                self.assertEqual({path.name for path in variants}, own)

    def test_catalog_offers_only_schools_with_a_foil(self):
        catalog = watcher._university_catalog(self.universities)
        listed = [l.split(":", 1)[0].strip() for l in catalog.splitlines() if l.strip()]
        self.assertEqual(set(listed), {u["id"] for u in self.universities})
        self.assertEqual(len(listed), len(set(listed)))  # each id once
        # A school that loses its foil is dropped from the offer entirely.
        victim = next(u for u in self.universities if u["id"] == "hspv-nrw")
        bare = {k: v for k, v in victim.items() if k != "templates"}
        bare["template"] = None
        modified = [u for u in self.universities if u["id"] != "hspv-nrw"] + [bare]
        self.assertNotIn("hspv-nrw", watcher._university_catalog(modified))


if __name__ == "__main__":
    unittest.main()
