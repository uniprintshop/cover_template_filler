import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

import reliability_cases as fixtures
import reliability_suite as suite
import thesis_cover_watch as pipeline
from cover_model import ModelUnavailable


class ReliabilityCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cfg = pipeline.load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        cls.templates = Path(__file__).resolve().parents[1] / "Prägungen"
        cls.corpus = cls.root / "corpus"
        cls.manifest = fixtures.build_corpus(cls.corpus, cls.templates, cls.cfg["universities"])
        cls.cases = {c["id"]: c for c in cls.manifest["cases"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_inventory_is_varied_and_files_match_manifest(self):
        loaded = suite.load_manifest(self.corpus)
        self.assertGreaterEqual(len(loaded["cases"]), 40)
        self.assertGreaterEqual(sum("logo-only" in c["tags"] for c in loaded["cases"]), 15)
        self.assertGreaterEqual(sum(c["expected"]["decision"] == "reject" for c in loaded["cases"]), 5)
        self.assertTrue((self.corpus / "contact_sheet.png").is_file())

    def test_every_positive_case_actually_contains_its_expected_institution(self):
        for case in self.manifest["cases"]:
            if case["expected"]["decision"] != "stamp":
                continue
            uid = case["expected"]["university_id"]
            matching = [p for p in case["pages"][:2] if p.get("institution_id") == uid
                        or any(mark["uid"] == uid for mark in p.get("logos", []))]
            with self.subTest(case=case["id"]):
                self.assertTrue(matching, "Ground truth must have visible evidence, not just an expected ID")
                digital = "\n".join(p.extract_text() or "" for p in PdfReader(self.corpus / case["pdf"]).pages[:2])
                if not any(p.get("logos") for p in matching) and not any("scan" in p for p in matching):
                    self.assertTrue(any(suite.canonical(p["institution_label"]) in suite.canonical(digital)
                                        for p in matching))

    def test_logo_cases_have_no_hidden_institution_text(self):
        for case in self.manifest["cases"]:
            if "logo-only" not in case["tags"]:
                continue
            digital = "\n".join(p.extract_text() or "" for p in PdfReader(self.corpus / case["pdf"]).pages[:2])
            with self.subTest(case=case["id"]):
                for token in ("hochschule", "universität", "university", "wuppertal", "bremen", "hspv", "rwth", "bochum"):
                    # Role labels may say Hochschule, but never transcribe a school identity.
                    if token == "hochschule" and case["id"] == "36_awarder_and_partner":
                        continue
                    self.assertNotIn(token, digital.casefold())

    def test_vector_logo_is_neither_embedded_image_nor_selectable_text(self):
        case = self.cases["11_vector_wordmark"]
        page = PdfReader(self.corpus / case["pdf"]).pages[0]
        self.assertEqual(len(page.images), 0)
        self.assertNotIn("Bremen", page.extract_text())
        self.assertIn(b" re f", page.get_contents().get_data())

    def test_large_banner_and_repeated_image_reproduce_extraction_edges(self):
        case = self.cases["05_logo_only"]
        image = PdfReader(self.corpus / case["pdf"]).pages[0].images[0].image
        self.assertGreater(image.width * image.height, 2_000_000)
        repeated = self.cases["12_repeated_wordmark"]
        pages = PdfReader(self.corpus / repeated["pdf"]).pages
        refs = [p["/Resources"]["/XObject"].raw_get("/I1").idnum for p in pages]
        self.assertEqual(refs[0], refs[1])

    def test_scans_have_no_selectable_text(self):
        for key in ("37_full_page_scan", "38_skewed_scan"):
            with self.subTest(case=key):
                page = PdfReader(self.corpus / self.cases[key]["pdf"]).pages[0]
                self.assertFalse((page.extract_text() or "").strip())
                self.assertEqual(len(page.images), 1)

    def test_live_runner_uses_isolated_outputs_and_can_resume(self):
        case = self.cases["01_digital_german"]
        expected = case["expected"]
        answer = {**expected["fields"], "university": expected["university_id"],
                  "degree_phrase": expected["fields"]["degree_text"]}
        results = self.root / "results"
        with patch.object(pipeline, "call_ollama", return_value=json.dumps(answer)) as backend:
            run_dir, summary = suite.run_suite(self.corpus, self.cfg, results_dir=results, rounds=1,
                                               selected=[case["id"]])
            self.assertEqual(summary["passed"], 1)
            self.assertEqual(backend.call_count, 1)
            _, resumed = suite.run_suite(self.corpus, self.cfg, resume=run_dir)
            self.assertEqual(backend.call_count, 1)
            self.assertEqual(resumed["passed"], 1)
        self.assertTrue((run_dir / "report.md").is_file())
        self.assertTrue((run_dir / "attempts.csv").is_file())
        record = suite.read_records(run_dir / "attempts.jsonl")[0]
        self.assertTrue(all(Path(o["path"]).is_relative_to(run_dir) for o in record["outputs"]))

    def test_actual_model_outage_is_not_a_successful_negative(self):
        case = self.cases["22_missing_school"]
        item = suite.schedule([case["id"]], 1, 1)[0]
        with patch.object(pipeline, "call_ollama", side_effect=ModelUnavailable("offline")):
            result = suite.run_attempt(case, item, self.corpus, self.root / "outage", self.cfg, self.templates, "2026")
        self.assertEqual(result["verdict"], "infrastructure_error")
        self.assertFalse(result["passed"])
        self.assertEqual(result["model_calls"], 1)

    def test_repeated_backend_outages_stop_before_exhausting_the_plan(self):
        chosen = ["01_digital_german", "02_english_cover", "03_unicode_author", "04_punctuation_title"]
        with patch.object(pipeline, "call_ollama", side_effect=ModelUnavailable("offline")) as backend:
            _, summary = suite.run_suite(self.corpus, self.cfg, results_dir=self.root / "outage-plan",
                                          rounds=1, selected=chosen)
        self.assertEqual(backend.call_count, 3)
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(summary["planned"], 4)
        self.assertEqual(summary["scored"], 0)


class ReliabilityScoringTests(unittest.TestCase):
    def expected(self):
        return {"decision": "stamp", "university_id": "correct", "fields": {
            "author": "Anna Weiß", "year": "2026", "title": "Title; subtitle"}}

    def fields(self):
        return {"university_id": "correct", "author": "Anna Weiß", "year": "2026", "title": "Title; subtitle"}

    def test_wrong_template_and_wrong_fields_are_distinct(self):
        fields = self.fields()
        fields["university_id"] = "wrong"
        self.assertEqual(suite.assess(self.expected(), fields, [{}])["verdict"], "wrong_template")
        fields = self.fields()
        fields["year"] = "1995"
        self.assertEqual(suite.assess(self.expected(), fields, [{}])["verdict"], "field_mismatch")
        self.assertEqual(suite.assess(self.expected(), self.fields(), [{"template": "Wrong.upf"}],
                                     approved={"Right.upf"})["verdict"], "wrong_template")

    def test_negative_cases_only_pass_for_real_validation_rejections(self):
        fields = {"risks": ["no_university_on_cover"], "model_outcome": {"status": "ok"}}
        expected = {"decision": "reject"}
        self.assertTrue(suite.assess(expected, fields, [], error_kind="rejection")["passed"])
        for error_kind in ("infrastructure", "model", "pipeline"):
            with self.subTest(kind=error_kind):
                self.assertFalse(suite.assess(expected, fields, [], error_kind=error_kind)["passed"])
        self.assertEqual(suite.assess(expected, fields, [{}], error_kind="infrastructure")["verdict"], "unsafe_accept")

    def test_refusing_positive_is_not_a_pass(self):
        fields = {"risks": ["no_university_on_cover"], "model_outcome": {"status": "ok"}}
        result = suite.assess(self.expected(), fields, [], error_kind="rejection")
        self.assertEqual(result["verdict"], "safe_rejection")
        self.assertFalse(result["passed"])

    def test_missing_author_validation_is_a_safe_refusal_not_a_parser_error(self):
        fields = {"risks": [], "model_outcome": {"status": "ok"}, "author": ""}
        result = suite.assess(self.expected(), fields, [], error_kind="rejection", error="Missing author or year")
        self.assertEqual(result["verdict"], "safe_rejection")
        self.assertFalse(result["passed"])

    def test_field_scoring_does_not_hide_punctuation_or_spelling_changes(self):
        fields = self.fields()
        fields["title"] = "Title subtitle"
        self.assertFalse(suite.assess(self.expected(), fields, [{}])["passed"])
        fields["title"] = "Title;\nsubtitle"
        self.assertTrue(suite.assess(self.expected(), fields, [{}])["passed"])

    def test_mixed_schedule_is_reproducible_and_has_every_round(self):
        plan = suite.schedule(["one", "two", "three"], 3, 42)
        self.assertEqual(plan, suite.schedule(["one", "two", "three"], 3, 42))
        self.assertEqual(len({item["attempt"] for item in plan}), 9)
        for number in (1, 2, 3):
            self.assertEqual({item["case"] for item in plan if item["round"] == number}, {"one", "two", "three"})
        self.assertTrue(all(a["case"] != b["case"] for a, b in zip(plan, plan[1:])))

    def test_torn_final_record_is_recovered_but_middle_corruption_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.jsonl"
            path.write_text('{"attempt":"one"}\n{"attempt":')
            self.assertEqual(suite.read_records(path), [{"attempt": "one"}])
            self.assertTrue((path.parent / "interrupted-final-row.txt").is_file())
            path.write_text('not json\n{"attempt":"two"}\n')
            with self.assertRaises(ValueError):
                suite.read_records(path)

    def test_result_paths_cannot_overlap_production(self):
        with self.assertRaises(ValueError):
            suite.ensure_isolated_results(Path("/production/out/run"), Path("/fixtures"), Path("/templates"),
                                          {"output_dir": "/production/out"})

    def test_configuration_report_omits_credentials(self):
        data = suite.safe_configuration({"model": {"backend": "openai", "api_key": "secret-value"}},
                                         Path("/templates"), {"publish_year": "2026"})
        self.assertNotIn("secret-value", json.dumps(data))

    def test_infrastructure_failures_are_excluded_from_accuracy(self):
        records = [{"case": "one", "verdict": "infrastructure_error", "tier": "core", "passed": False,
                    "seconds": 1, "timing_resumed": False, "tags": [], "fields": {}, "model_calls": 1}]
        summary = suite.summarize(records, 1)
        self.assertEqual(summary["scored"], 0)
        self.assertEqual(summary["passed"], 0)

    def test_initial_model_accuracy_is_separate_from_repaired_output(self):
        case = {"expected": self.expected()}
        fields = {**self.fields(), "model_raw": {"university": "wrong", "author": "Anna Weiß",
                                                  "year": "2026", "title": "Title; subtitle"}}
        observation = suite.initial_model_assessment(case, {"fields": fields})
        self.assertFalse(observation["passed"])
        self.assertFalse(observation["fields_correct"]["university"])

    def test_missing_year_raw_answer_must_be_empty_before_business_fallback(self):
        case = {"expected": {**self.expected(), "required_risks": ["year_from_publish_year_default"]}}
        fields = {"model_raw": {"university": "correct", "author": "Anna Weiß", "year": "", "title": "Title; subtitle"}}
        self.assertTrue(suite.initial_model_assessment(case, {"fields": fields})["passed"])

    def test_two_runners_cannot_write_the_same_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with suite.single_runner(Path(tmp)):
                with self.assertRaises(ValueError):
                    with suite.single_runner(Path(tmp)):
                        self.fail("Concurrent experiment ownership was allowed")
