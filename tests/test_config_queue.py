import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from cover_config import normalize_config
from cover_queue import PdfQueue
import thesis_cover_watch as w


class ConfigTests(unittest.TestCase):
    def base(self):
        return {"watch_dir": "in", "output_dir": "out", "templates_dir": "templates"}

    def test_defaults_are_effective_and_paths_are_config_relative(self):
        cfg = normalize_config(self.base(), Path("/tmp/config-test"))
        self.assertEqual(cfg["processed_dir"], "/tmp/config-test/in/processed")
        self.assertEqual(cfg["failed_dir"], "/tmp/config-test/in/failed")
        self.assertEqual(cfg["watch_rescan_interval"], 60)

    def test_empty_variant_list_uses_primary_consistently(self):
        raw = {**self.base(), "universities": [{"id": "one", "template": "One.upf", "templates": []}]}
        self.assertEqual(normalize_config(raw, Path("/tmp"))["universities"][0]["templates"], ["One.upf"])

    def test_invalid_backend_limits_and_duplicate_ids_are_rejected(self):
        examples = [{"model": {"backend": "typo"}}, {"ocr_timeout_seconds": -1},
                    {"universities": [{"id": "same"}, {"id": "same"}]}]
        for values in examples:
            with self.subTest(values=values), self.assertRaises(ValueError):
                normalize_config({**self.base(), **values}, Path("/tmp"))


class QueueTests(unittest.TestCase):
    def test_events_and_rescans_share_a_deduplicated_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "cover.Pdf"
            pdf.touch()
            work = PdfQueue(lambda path: True, lambda *args: True)
            self.assertTrue(work.submit(pdf))
            self.assertFalse(work.submit(pdf, "scan"))
            w.scan_existing({"watch_dir": tmp}, work)
            self.assertEqual(work.items.qsize(), 1)

    def test_transient_failure_retries_without_another_file_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "cover.pdf"
            pdf.touch()
            done = threading.Event()
            calls = []

            def handle(path, origin, timings):
                calls.append(timings)
                if len(calls) == 1:
                    return False
                done.set()
                return True

            work = PdfQueue(lambda path: True, handle, retry_seconds=.01)
            work.submit(pdf)
            work.start()
            try:
                self.assertTrue(done.wait(3))
            finally:
                work.stop()
            self.assertEqual(len(calls), 2)
            self.assertIn("settle", calls[0])

    def test_queue_limit_does_not_grow_unbounded(self):
        work = PdfQueue(Mock(), Mock(), limit=1)
        self.assertTrue(work.submit(Path("one.pdf")))
        self.assertFalse(work.submit(Path("two.pdf")))
