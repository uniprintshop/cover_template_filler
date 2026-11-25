import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import cover_model as model
import thesis_cover_watch as w


class ModelFailureTests(unittest.TestCase):
    def test_http_error_preserves_server_explanation(self):
        error = urllib.error.HTTPError("http://model/api/chat", 400, "Bad Request", {},
                                      io.BytesIO(b'{"error":"Failed to load image"}'))
        with patch.object(model.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(model.InvalidModelResponse, "Failed to load image"):
                model.request_json(urllib.request.Request("http://model/api/chat"), 1)

    def test_unavailable_model_does_not_trigger_ocr_or_report_inference(self):
        cfg = {"model": {"backend": "ollama"}}
        with patch.object(w, "get_cover_text", return_value=("Cover text " * 10, "digital")), \
                patch.object(w, "pdf_metadata", return_value={}), \
                patch.object(w, "call_ollama", side_effect=model.ModelUnavailable("offline")) as call, \
                patch.object(w, "extract_ocr") as ocr, patch.object(w, "write_fail_report") as report:
            with self.assertRaises(model.ModelUnavailable):
                w.process_pdf(Path("cover.pdf"), cfg)
        self.assertEqual(call.call_count, 1)
        ocr.assert_not_called()
        report.assert_not_called()

    def test_failure_report_never_calls_a_model(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(w, "_local_fail_dir", return_value=Path(tmp)), \
                patch.object(w, "call_ollama") as call:
            result = w.write_fail_report(Path("cover.pdf"), {}, ValueError("bad template"), "Cover text")
            self.assertIn("bad template", result.read_text())
            call.assert_not_called()

    def test_invalid_field_shapes_are_explicit(self):
        with patch.object(w, "call_ollama", return_value=json.dumps({"author": ["Someone"]})):
            with self.assertRaises(model.InvalidModelResponse):
                w._model_fields("Cover", {}, {"model": {"backend": "ollama"}}, [], {})

    def test_empty_or_truncated_responses_are_explicit(self):
        for content, reason in (("", "stop"), ("partial", "length")):
            with self.subTest(reason=reason), self.assertRaises(model.InvalidModelResponse):
                model.require_content(content, reason)
