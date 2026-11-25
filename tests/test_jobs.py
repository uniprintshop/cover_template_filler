import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cover_jobs as jobs
import thesis_cover_watch as w


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "cover.pdf"
        self.source.write_bytes(b"test source")
        self.cfg = {"state_dir": str(self.root / "state"), "notify": False,
                    "processed_dir": str(self.root / "processed"),
                    "output_dir": str(self.root / "out")}

    def test_archive_failure_never_repeats_generation(self):
        with patch.object(w, "_process_pdf_inner", return_value=[self.root / "cover.upf"]) as generate, \
                patch.object(w, "move_aside", side_effect=OSError("share offline")):
            w.handle_pdf(self.source, self.cfg, "test")
            w.handle_pdf(self.source, self.cfg, "test")
        self.assertEqual(generate.call_count, 1)

    def test_replaced_source_is_a_new_job(self):
        with patch.object(w, "_process_pdf_inner", return_value=[]) as generate:
            w.process_pdf(self.source, self.cfg)
            self.source.write_bytes(b"new content")
            w.process_pdf(self.source, self.cfg)
        self.assertEqual(generate.call_count, 2)

    def test_force_creates_a_new_generation(self):
        first = jobs.JobState(self.source, "digest", self.cfg)
        first.complete([])
        second = jobs.JobState(self.source, "digest", {**self.cfg, "force": True})
        self.assertNotEqual(first.record["generation"], second.record["generation"])
        self.assertFalse(second.record["completed"])

    def test_publish_recovers_crash_between_rename_and_checkpoint(self):
        state = jobs.JobState(self.source, "digest", self.cfg)
        output = self.root / "cover.upf"
        output.write_bytes(b"complete output")
        state.publish(output, b"complete output")
        self.assertIn(str(output), state.record["published"])
        with self.assertRaisesRegex(RuntimeError, "changed during retry"):
            state.publish(output, b"different data")

    def test_failed_atomic_write_preserves_existing_file(self):
        output = self.root / "cover.upf"
        output.write_bytes(b"original")
        with patch.object(jobs.os, "replace", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                jobs.atomic_write(output, b"replacement")
        self.assertEqual(output.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_snapshot_is_local_and_matches_source(self):
        with jobs.staged_pdf(self.source) as (local, digest):
            self.assertNotEqual(local, self.source)
            self.assertEqual(local.read_bytes(), b"test source")
            self.assertEqual(len(digest), 64)
        self.assertFalse(local.exists())

    def test_sidecar_retry_reuses_prepared_fields_and_existing_cover(self):
        template = "Westfälische_Hochschule_Gelsenkirchen.upf"
        cfg = {**self.cfg, "templates_dir": str(Path(__file__).resolve().parents[1] / "Prägungen"),
               "universities": [{"id": "test", "template": template}]}
        fields = {"university_id": "test", "university_template": template,
                  "author": "Audit Example", "year": "2026", "degree_text": "Masterarbeit",
                  "notes": [], "risks": [], "ok_to_stamp": True}
        original = jobs.JobState.publish
        failed = []

        def publish(state, path, data):
            if path.suffix == ".json" and not failed:
                failed.append(True)
                raise OSError("share interrupted")
            return original(state, path, data)

        with patch.object(w, "_prepare_pdf", return_value=(fields, "digital", 0, 0)) as prepare, \
                patch.object(jobs.JobState, "publish", publish), \
                patch.object(w, "retry_io", side_effect=lambda fn, **kwargs: fn()):
            with self.assertRaises(jobs.RetryableJobError):
                w.process_pdf(self.source, cfg)
            w.process_pdf(self.source, cfg)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(len(list((self.root / "out").glob("*.upf"))), 1)
        self.assertEqual(len(list((self.root / "out").glob("*.json"))), 1)
