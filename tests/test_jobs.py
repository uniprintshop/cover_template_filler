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
        outputs = [self.root / "out" / "cover.upf"]
        outputs[0].parent.mkdir(parents=True, exist_ok=True)

        def generate_and_write(*args, **kwargs):
            outputs[0].write_bytes(b"cover")
            return outputs

        with self.assertLogs(w.LOG, level="ERROR"), \
                patch.object(w, "_process_pdf_inner", side_effect=generate_and_write) as generate, \
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

    def test_missing_outputs_are_regenerated(self):
        outputs = [self.root / "out" / "cover.upf"]
        outputs[0].parent.mkdir(parents=True, exist_ok=True)

        def generate_and_write(*args, **kwargs):
            outputs[0].write_bytes(b"cover")
            return outputs

        with patch.object(w, "_process_pdf_inner", side_effect=generate_and_write) as generate, \
                patch.object(w, "move_aside"):
            w.process_pdf(self.source, self.cfg)
            outputs[0].write_bytes(b"placeholder")
            self.assertEqual(generate.call_count, 1)
            outputs[0].unlink()
            w.process_pdf(self.source, self.cfg)
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(outputs[0].exists())

    def test_existing_outputs_still_skip_generation(self):
        outputs = [self.root / "out" / "cover.upf"]
        outputs[0].parent.mkdir(parents=True, exist_ok=True)

        def generate_and_write(*args, **kwargs):
            outputs[0].write_bytes(b"cover")
            return outputs

        with patch.object(w, "_process_pdf_inner", side_effect=generate_and_write) as generate, \
                patch.object(w, "move_aside"):
            w.process_pdf(self.source, self.cfg)
            w.process_pdf(self.source, self.cfg)
        self.assertEqual(generate.call_count, 1)

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

    def test_published_permissions_match_normal_files_but_journals_stay_private(self):
        normal = self.root / "normal.upf"
        normal.write_bytes(b"normal")
        state = jobs.JobState(self.source, "digest", self.cfg)
        published = self.root / "published.upf"
        state.publish(published, b"published")
        self.assertEqual(published.stat().st_mode & 0o777, normal.stat().st_mode & 0o777)
        self.assertEqual(state.path.stat().st_mode & 0o077, 0)

    def test_snapshot_is_local_and_matches_source(self):
        with jobs.staged_pdf(self.source) as (local, digest):
            self.assertNotEqual(local, self.source)
            self.assertEqual(local.read_bytes(), b"test source")
            self.assertEqual(len(digest), 64)
        self.assertFalse(local.exists())

    def test_source_replacement_during_processing_is_not_archived(self):
        def replace_source(*args):
            self.source.write_bytes(b"different new PDF contents")
            return []

        with patch.object(w, "process_pdf", side_effect=replace_source), patch.object(w, "move_aside") as move:
            self.assertFalse(w.handle_pdf(self.source, self.cfg, "test"))
        move.assert_not_called()

    def test_journal_lock_prevents_parallel_ownership(self):
        with jobs.job_lock(self.source, self.cfg):
            with self.assertRaises(jobs.RetryableJobError):
                with jobs.job_lock(self.source, self.cfg):
                    self.fail("Lock was acquired twice")

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

    def test_share_write_failure_does_not_retry_every_variant(self):
        templates = ["Westfälische_Hochschule_Gelsenkirchen.upf", "Bremen_Universität.upf"]
        cfg = {**self.cfg, "templates_dir": str(Path(__file__).resolve().parents[1] / "Prägungen"),
               "universities": [{"id": "test", "template": templates[0], "templates": templates}]}
        fields = {"university_id": "test", "university_template": templates[0],
                  "author": "Audit Example", "year": "2026", "degree_text": "Masterarbeit",
                  "notes": [], "risks": [], "ok_to_stamp": True}
        with patch.object(w, "_prepare_pdf", return_value=(fields, "digital", 0, 0)), \
                patch.object(w, "_write_upf", side_effect=OSError("share offline")) as publish:
            with self.assertRaises(jobs.RetryableJobError):
                w.process_pdf(self.source, cfg)
        self.assertEqual(publish.call_count, 1)
