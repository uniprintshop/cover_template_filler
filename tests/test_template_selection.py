"""Selection regressions; run with python -m unittest discover -s tests."""

import copy
import json
import os
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
            for name in university.get("templates", [university.get("template")]):
                if name:
                    (self.directory / name).touch()
        self.config["templates_dir"] = str(self.directory)
        self.config["model"] = {"backend": "none"}
        watcher._TPL_CACHE.clear()
        watcher._UNI_HITS_CACHE.clear()

    def test_every_configured_alias_resolves_to_its_institution(self):
        for university in self.universities:
            for alias in university["aliases"]:
                with self.subTest(university=university["id"], alias=alias):
                    resolved, _, _ = watcher.resolve_university(
                        alias, alias, self.universities, self.directory
                    )
                    self.assertIsNotNone(resolved)
                    self.assertEqual(resolved["id"], university["id"])

    def test_city_neighbors_are_not_variants(self):
        for university in self.universities:
            with self.subTest(university=university["id"]):
                approved = set(university.get("templates", [university.get("template")])) - {"", None}
                grounded = watcher.grounded_templates(
                    university["aliases"][0], self.directory, self.universities
                )
                self.assertEqual({path.name for path in grounded}, approved)
                # Even a model-selected neighbor cannot enlarge membership.
                variants = watcher.template_variants_for(
                    university, "Leipzig_Universität.upf", self.directory
                )
                self.assertEqual({path.name for path in variants}, approved)

    def test_deleted_school_never_searches_for_another_logo(self):
        for ident in ("htwk", "barmen", "berufskolleg-elberfeld"):
            university = next(u for u in self.universities if u["id"] == ident)
            cover = f"{university['aliases'][0]}\nBachelorarbeit\nVorgelegt von: Lena Hoffmann\n2026"
            with self.subTest(university=ident), patch.object(watcher, "model_search_templates") as search:
                fields = watcher.extract_fields(cover, {}, copy.deepcopy(self.config))
                self.assertEqual(fields["university_template"], "")
                self.assertIn("no_template_for_university", fields["risks"])
                self.assertFalse(fields["ok_to_stamp"])
                search.assert_not_called()

    def test_missing_primary_uses_existing_approved_variant(self):
        university = next(u for u in self.universities if u["id"] == "wuppertal")
        (self.directory / university["template"]).unlink()
        selected = watcher.hardcoded_template_path(university, self.directory)
        self.assertEqual(selected.name, university["templates"][1])

    def test_directory_changes_invalidate_catalog(self):
        before = {path.name for path in watcher.list_plain_templates(self.directory)}
        source = self.directory / "Leipzig_Universität.upf"
        source.rename(self.directory / "Renamed.upf")
        # Ensure a distinct directory timestamp even on coarse filesystems.
        stat = self.directory.stat()
        os.utime(self.directory, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        after = {path.name for path in watcher.list_plain_templates(self.directory)}
        self.assertEqual(after, before - {source.name} | {"Renamed.upf"})

    def test_ambiguous_filename_family_is_not_arbitrarily_chosen(self):
        paths = [self.directory / "1-School.upf", self.directory / "2-School.upf"]
        self.assertIsNone(watcher._match_template_guess("School.upf", paths))
        self.assertEqual(watcher._match_template_guess("2-School.upf", paths), paths[1])


if __name__ == "__main__":
    unittest.main()
