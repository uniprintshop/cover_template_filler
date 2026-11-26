#!/usr/bin/env python3
"""Build synthetic covers and benchmark the real pipeline against fixed answers."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import thesis_cover_watch as pipeline
from cover_jobs import RetryableJobError, atomic_write
from cover_model import InvalidModelResponse, ModelUnavailable
from reliability_cases import SCHEMA, build_corpus

ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS = ROOT / "reliability_runs/corpus"
DEFAULT_RESULTS = ROOT / "reliability_runs/results"


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    """Whitespace-only normalization; spelling, case, and punctuation still count."""
    return " ".join(str(value or "").split())


def load_manifest(root):
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA or manifest.get("synthetic") is not True:
        raise ValueError("Unsupported or non-synthetic corpus manifest")
    seen = set()
    for case in manifest["cases"]:
        if case["id"] in seen:
            raise ValueError(f"Duplicate case ID: {case['id']}")
        seen.add(case["id"])
        path = (root / case["pdf"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".pdf":
            raise ValueError("Fixture path leaves the generated corpus")
        if digest_file(path) != case["sha256"]:
            raise ValueError(f"Fixture changed since generation: {path}")
        if case["expected"]["decision"] not in ("stamp", "reject"):
            raise ValueError(f"Invalid expected decision for {case['id']}")
    return manifest


def schedule(case_ids, rounds, seed, order="mixed"):
    if rounds < 1:
        raise ValueError("rounds must be positive")
    rng = random.Random(seed)
    result = []
    if order == "repeat":
        for case_id in case_ids:
            result.extend({"case": case_id, "round": number} for number in range(1, rounds + 1))
    else:
        previous = None
        for number in range(1, rounds + 1):
            shuffled = list(case_ids)
            rng.shuffle(shuffled)
            if len(shuffled) > 1 and shuffled[0] == previous:
                shuffled = shuffled[1:] + shuffled[:1]
            result.extend({"case": case_id, "round": number} for case_id in shuffled)
            previous = shuffled[-1]
    for index, item in enumerate(result, 1):
        item["attempt"] = f"{index:03d}_r{item['round']:02d}_{item['case']}"
    return result


def assess(expected, fields, outputs, *, error_kind=None, error="", rendered=None, approved=None):
    """A failed API request can never pass a negative test by accident."""
    mismatches = {}
    published = bool(outputs)
    if expected["decision"] == "reject" and published:
        return {"verdict": "unsafe_accept", "passed": False, "mismatches": {"decision": "stamped a required rejection"}}
    if error_kind == "infrastructure":
        return {"verdict": "infrastructure_error", "passed": False, "mismatches": {"error": error}}
    if error_kind == "model":
        return {"verdict": "model_error", "passed": False, "mismatches": {"error": error}}
    if not published:
        risks = fields.get("risks", [])
        valid_refusal = any(r in ("no_university_on_cover", "university_not_on_cover", "university_ambiguous")
                            or r.startswith("university_id_not_in_catalog:") for r in risks)
        if expected["decision"] == "stamp" and error_kind == "rejection":
            valid_refusal = True
        if fields.get("model_outcome", {}).get("status") not in (None, "ok"):
            valid_refusal = False
        if error_kind not in (None, "rejection") or not valid_refusal:
            verdict = "pipeline_error"
        else:
            verdict = "correct_rejection" if expected["decision"] == "reject" else "safe_rejection"
        return {"verdict": verdict, "passed": verdict == "correct_rejection", "mismatches": {"error": error}}
    if error_kind:
        return {"verdict": "pipeline_error", "passed": False, "mismatches": {"error": error}}
    if fields.get("university_id") != expected["university_id"]:
        return {"verdict": "wrong_template", "passed": False,
                "mismatches": {"university_id": {"expected": expected["university_id"], "actual": fields.get("university_id")}}}
    if approved is not None and any(output.get("template") not in approved for output in outputs):
        return {"verdict": "wrong_template", "passed": False,
                "mismatches": {"templates": "A produced template does not belong to the expected institution"}}
    for key, value in expected.get("fields", {}).items():
        if canonical(fields.get(key)) != canonical(value):
            mismatches[key] = {"expected": value, "actual": fields.get(key)}
    for risk in expected.get("required_risks", []):
        if risk not in fields.get("risks", []):
            mismatches["required_risk:" + risk] = "not reported"
    if rendered is not None:
        for key in ("author", "year"):
            value = canonical(expected.get("fields", {}).get(key))
            for path, value_on_cover in rendered.items():
                if value and value not in canonical(value_on_cover):
                    mismatches[f"rendered:{key}:{Path(path).name}"] = "expected text missing from UPF"
    return {"verdict": "field_mismatch" if mismatches else "correct_stamp",
            "passed": not mismatches, "mismatches": mismatches}


class InferenceLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.calls = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("Inference metrics: "):
            self.calls.append(json.loads(message.partition(": ")[2]))


def isolated_config(cfg, attempt_dir, templates_dir, publish_year):
    runtime = deepcopy(cfg)
    for key, folder in (("watch_dir", "input"), ("output_dir", "outputs"), ("processed_dir", "processed"),
                        ("failed_dir", "failed"), ("local_fail_dir", "failed-local"), ("state_dir", "state")):
        runtime[key] = str((attempt_dir / folder).resolve())
        if not Path(runtime[key]).is_relative_to(attempt_dir.resolve()):
            raise ValueError("Attempt paths must stay inside the experiment")
    runtime.update(templates_dir=str(templates_dir.resolve()), notify=False, force=False, publish_year=publish_year)
    return runtime


def approved_templates(uid, cfg, templates_dir):
    uni = next((u for u in cfg["universities"] if u["id"] == uid), None)
    if not uni:
        return set()
    names = set(uni.get("templates") or [uni.get("template")]) - {None, ""}
    if cfg.get("write_title_variant"):
        for name in list(names):
            _, sibling = pipeline.find_template_pair(name, templates_dir)
            if sibling:
                names.add(sibling.name)
    return names


def run_attempt(case, item, corpus, run_dir, cfg, templates_dir, publish_year):
    attempt_dir = run_dir / "attempts" / item["attempt"]
    attempt_dir.mkdir(parents=True, exist_ok=True)
    runtime = isolated_config(cfg, attempt_dir, templates_dir, publish_year)
    pdf = corpus / case["pdf"]
    timing_resumed = bool(list((attempt_dir / "state/jobs").glob("*.json")))
    original = pipeline._process_pdf_inner
    ctx = {}

    def observe(path, settings, context):
        try:
            return original(path, settings, context)
        finally:
            ctx.update(deepcopy(context))

    logger = pipeline.LOG
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logfile = logging.FileHandler(attempt_dir / "pipeline.log", encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    metrics = InferenceLog()
    logger.addHandler(logfile)
    logger.addHandler(metrics)
    error, error_kind = "", None
    started = time.monotonic()
    try:
        # These wrappers observe actual calls; no model answers are stubbed.
        with patch.object(pipeline, "_process_pdf_inner", side_effect=observe), \
                patch.object(pipeline, "call_ollama", wraps=pipeline.call_ollama) as ollama, \
                patch.object(pipeline, "call_openai", wraps=pipeline.call_openai) as openai:
            try:
                pipeline.process_pdf(pdf, runtime)
            except (ModelUnavailable, RetryableJobError) as exc:
                error_kind, error = "infrastructure", f"{type(exc).__name__}: {exc}"
            except InvalidModelResponse as exc:
                error_kind, error = "model", f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                refusal = type(exc) is RuntimeError and str(exc).startswith((
                    "No awarding institution identified", "Model identified '", "Model returned university id '",
                    "Missing author or year:", "Author looks like a supervisor", "Several years on the cover",
                    "Validated fields are not safe to stamp:",
                ))
                error_kind, error = "rejection" if refusal else "pipeline", f"{type(exc).__name__}: {exc}"
            requests = ollama.call_count + openai.call_count
    finally:
        seconds = time.monotonic() - started
        logger.removeHandler(logfile)
        logger.removeHandler(metrics)
        logfile.close()
        logger.setLevel(old_level)
    sidecars = sorted((attempt_dir / "outputs").glob("*.json"))
    data = json.loads(sidecars[0].read_text()) if sidecars else {}
    fields = data.get("fields", ctx.get("fields", {}))
    files = sorted((attempt_dir / "outputs").glob("*.upf"))
    output_metadata = {Path(o["path"]).name: o for o in data.get("outputs", [])}
    outputs, rendered = [], {}
    for path in files:
        outputs.append(output_metadata.get(path.name, {"path": str(path), "template": None, "partial": True}))
        try:
            rendered[str(path)] = " ".join(b.joined() for b in pipeline.find_text_blocks(path.read_text()))
        except Exception as exc:
            error_kind, error = "pipeline", f"Could not reparse produced UPF: {exc}"
    if digest_file(pdf) != case["sha256"]:
        raise RuntimeError(f"Source fixture was modified: {pdf}")
    assessment = assess(case["expected"], fields, outputs, error_kind=error_kind, error=error,
                        rendered=rendered, approved=approved_templates(case["expected"].get("university_id"), cfg, templates_dir))
    calls = metrics.calls or fields.get("inference_metrics", [])
    return {**item, **assessment, "tier": case["tier"], "tags": case["tags"],
            "seconds": round(seconds, 3), "timing_resumed": timing_resumed,
            "source": data.get("text_source", ctx.get("source")), "fields": fields,
            "outputs": outputs, "error_kind": error_kind, "error": error,
            "model_calls": max(requests, len(calls)), "requests_this_invocation": requests,
            "inference": calls, "extraction": fields.get("extraction_timings", {}),
            "pdf": str(pdf), "log": str(attempt_dir / "pipeline.log")}


def latency(values):
    values = sorted(values)
    if not values:
        return {"n": 0, "median": None, "p95": None, "min": None, "max": None}
    return {"n": len(values), "median": round(statistics.median(values), 3),
            "p95": round(values[max(0, math.ceil(.95 * len(values)) - 1)], 3),
            "min": round(values[0], 3), "max": round(values[-1], 3)}


def summarize(records, planned):
    counts = Counter(r["verdict"] for r in records)
    scored = [r for r in records if r["verdict"] != "infrastructure_error"]
    by_tier = {}
    for tier in ("core", "stress", "capability"):
        group = [r for r in scored if r["tier"] == tier]
        by_tier[tier] = {"scored": len(group), "passed": sum(r["passed"] for r in group)}
    signatures, raw_signatures, by_case, by_tag = defaultdict(set), defaultdict(set), defaultdict(list), defaultdict(list)
    stages, by_round = defaultdict(list), defaultdict(list)
    for record in records:
        by_case[record["case"]].append(record)
        if "round" in record:
            by_round[record["round"]].append(record)
        if not record["timing_resumed"]:
            for key, seconds in record.get("extraction", {}).items():
                if isinstance(seconds, (int, float)) and math.isfinite(seconds):
                    stages[key].append(seconds)
            calls = record.get("inference", [])
            if calls and len(calls) == record["model_calls"]:
                for key, scale in (("wall_seconds", 1), ("prompt_eval_duration", 1e-9), ("eval_duration", 1e-9)):
                    values = [call.get(key) for call in calls]
                    if all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
                        stages["inference_" + key].append(sum(values) * scale)
        for tag in record["tags"]:
            by_tag[tag].append(record)
        raw = record["fields"].get("model_raw")
        if raw:
            raw_signatures[record["case"]].add(tuple(canonical(raw.get(key)) for key in (
                "university", "author", "year", "title", "degree_type")))
        if record["verdict"] == "infrastructure_error":
            continue
        identity = ("reject",) if record["verdict"] == "correct_rejection" else (
            record["verdict"], *(canonical(record["fields"].get(k)) for k in
                                   ("university_id", "author", "year", "title", "degree_text")))
        signatures[record["case"]].add(identity)
    initial = [r["initial_model"] for r in records if r.get("initial_model", {}).get("scored")]
    raw_fields = defaultdict(lambda: {"correct": 0, "scored": 0})
    for observation in initial:
        for key, matched in observation.get("fields_correct", {}).items():
            raw_fields[key]["scored"] += 1
            raw_fields[key]["correct"] += int(matched)
    return {"planned": planned, "completed": len(records), "scored": len(scored),
            "passed": sum(r["passed"] for r in scored), "verdicts": dict(counts), "by_tier": by_tier,
            "safety": {"wrong_or_unapproved_templates": counts["wrong_template"],
                       "stamped_required_rejections": counts["unsafe_accept"],
                       "wrong_author_or_year": sum(any(k in r.get("mismatches", {}) for k in ("author", "year")) for r in records),
                       "wrong_degree": sum(any(k in r.get("mismatches", {}) for k in ("degree_text", "degree_type")) for r in records)},
            "initial_model": {"scored": len(initial), "fully_correct": sum(o["passed"] for o in initial),
                              "by_field": dict(raw_fields)},
            "latency": latency([r["seconds"] for r in records if not r["timing_resumed"]]),
            "observed_processing_seconds": round(sum(r["seconds"] for r in records if not r["timing_resumed"]), 3),
            "successful_latency": latency([r["seconds"] for r in records if r["passed"] and not r["timing_resumed"]]),
            "model_calls": sum(r["model_calls"] for r in records),
            "unstable_cases": sorted(k for k, values in signatures.items() if len(values) > 1),
            "initial_model_unstable_cases": sorted(k for k, values in raw_signatures.items() if len(values) > 1),
            "repeated_cases": sum(len([r for r in group if r["verdict"] != "infrastructure_error"]) > 1
                                  for group in by_case.values()),
            "stage_latency_seconds": {key: latency(values) for key, values in sorted(stages.items())},
            "by_round": {str(number): {"attempts": len(group), "passed": sum(r["passed"] for r in group),
                                       "latency": latency([r["seconds"] for r in group if not r["timing_resumed"]])}
                         for number, group in sorted(by_round.items())},
            "by_tag": {tag: {"attempts": len(group), "passed": sum(r["passed"] for r in group),
                              "latency": latency([r["seconds"] for r in group if not r["timing_resumed"]])}
                       for tag, group in sorted(by_tag.items())}}


def initial_model_assessment(case, record):
    raw = record["fields"].get("model_raw")
    if not raw:
        return {"scored": False, "passed": False, "fields_correct": {}}
    expected = {"university": case["expected"].get("university_id") or ""}
    if case["expected"]["decision"] == "stamp":
        expected.update({key: value for key, value in case["expected"]["fields"].items()
                         if key in ("author", "year", "title", "degree_type")})
        if "year_from_publish_year_default" in case["expected"].get("required_risks", []):
            expected["year"] = ""  # A model must not invent a year missing from the cover.
    matched = {key: canonical(raw.get(key)) == canonical(value) for key, value in expected.items()}
    return {"scored": True, "passed": all(matched.values()), "fields_correct": matched,
            "mismatches": {key: {"expected": expected[key], "actual": raw.get(key)}
                           for key, correct in matched.items() if not correct}}


def write_reports(run_dir, records, plan):
    # Also supports reports from interrupted/older harness executions whose
    # immutable attempt records already contain the initial parsed response.
    manifest = json.loads((Path(plan["corpus"]) / "manifest.json").read_text())
    cases = {case["id"]: case for case in manifest["cases"]}
    records = [{**record, "initial_model": initial_model_assessment(cases[record["case"]], record)} for record in records]
    summary = summarize(records, len(plan["schedule"]))
    atomic_write(run_dir / "summary.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
    lines = ["# Synthetic-cover reliability results", "",
             f"Completed **{summary['completed']}/{summary['planned']}** attempts; **{summary['passed']}/{summary['scored']}** scored attempts passed.",
             "Infrastructure failures are excluded from accuracy, never counted as correct refusals.", "",
             "| Group | Passed | Scored |", "|---|---:|---:|"]
    for tier, value in summary["by_tier"].items():
        lines.append(f"| {tier} | {value['passed']} | {value['scored']} |")
    lines += ["", "## Outcomes", "", "| Verdict | Count |", "|---|---:|"]
    lines += [f"| {name} | {count} |" for name, count in sorted(summary["verdicts"].items())]
    lines += ["", "## Stamped-content safety", "", "```json",
              json.dumps(summary["safety"], ensure_ascii=False, indent=2), "```", "",
              "## Initial model answers before validation/recovery", "",
              f"Fully correct: **{summary['initial_model']['fully_correct']}/{summary['initial_model']['scored']}** parsed initial answers.",
              "This measures the model under the extracted input, not image-understanding in isolation. "
              "OCR-rescue answers are not substituted for the initial response; transport/empty-response failures are reported separately.",
              "", "| Field | Correct | Scored |", "|---|---:|---:|"]
    for name, values in summary["initial_model"]["by_field"].items():
        lines.append(f"| {name} | {values['correct']} | {values['scored']} |")
    lines += ["", "| Case | Round | Initial differing fields | Final pipeline outcome |", "|---|---:|---|---|"]
    for record in records:
        raw = record.get("initial_model", {})
        if raw.get("scored") and not raw.get("passed"):
            lines.append(f"| {record['case']} | {record['round']} | {', '.join(raw.get('mismatches', {}))} | {record['verdict']} |")
    timing = summary["latency"]
    lines += ["", "## Time and consistency", "",
              f"Wall time: median **{timing['median']}s**, p95 **{timing['p95']}s**, range **{timing['min']}–{timing['max']}s**.",
              f"Observed processing time across non-resumed attempts: **{summary['observed_processing_seconds']}s**.",
              f"Model calls: **{summary['model_calls']}**. Cases with repeated scored observations: **{summary['repeated_cases']}**.",
              "Unstable cases: " + (", ".join(summary["unstable_cases"]) or "none observed"),
              "Initial model-answer variations: " + (", ".join(summary["initial_model_unstable_cases"]) or "none observed"),
              "", "One observation cannot establish repeatability. Resumed/interrupted trial timings are excluded.",
              "Core, stress, and symbol-only capability probes are reported separately; safe refusals of positive cases remain failures.",
              "", "| Stage (seconds) | Observations | Median | p95 |", "|---|---:|---:|---:|"]
    for key, values in summary["stage_latency_seconds"].items():
        lines.append(f"| {key} | {values['n']} | {values['median']} | {values['p95']} |")
    lines += [
              "", "## Every attempt", "", "| Case | Round | Outcome | Seconds | Calls | Recorded institution ID |",
              "|---|---:|---|---:|---:|---|"]
    for record in records:
        lines.append(f"| {record['case']} | {record['round']} | {record['verdict']} | {record['seconds']} | "
                     f"{record['model_calls']} | {record['fields'].get('university_id', '')} |")
    lines += ["", "## Failure details", ""]
    for record in records:
        if record["passed"]:
            continue
        lines += [f"### {record['attempt']}: {record['verdict']}", "",
                  f"PDF: `{record['pdf']}`  ", f"Log: `{record['log']}`", "", "```json",
                  json.dumps(record["mismatches"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## Configuration", "", "```json", json.dumps(plan["configuration"], ensure_ascii=False, indent=2), "```", "",
              "Synthetic layouts are controlled tests, not a substitute for a curated corpus of real customer PDFs.",
              "Mixed rounds reduce immediate-repeat cache bias; server contention and cache residency remain uncontrolled.", ""]
    atomic_write(run_dir / "report.md", "\n".join(lines).encode())
    stream = io_csv(records)
    atomic_write(run_dir / "attempts.csv", stream.encode())
    return summary


def io_csv(records):
    import io
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["case", "round", "verdict", "passed", "seconds", "model_calls", "source", "university", "author", "year",
                     "mismatches", "initial_model_correct", "initial_model_mismatches"])
    for record in records:
        writer.writerow([record["case"], record["round"], record["verdict"], record["passed"], record["seconds"],
                         record["model_calls"], record["source"], record["fields"].get("university_id"),
                         record["fields"].get("author"), record["fields"].get("year"),
                         json.dumps(record["mismatches"], ensure_ascii=False), record.get("initial_model", {}).get("passed"),
                         json.dumps(record.get("initial_model", {}).get("mismatches", {}), ensure_ascii=False)])
    return stream.getvalue()


def safe_configuration(cfg, templates_dir, manifest):
    keys = ("backend", "name", "host", "openai_base", "think", "num_ctx", "num_thread", "timeout", "keep_alive")
    model = {k: cfg.get("model", {})[k] for k in keys if k in cfg.get("model", {})}
    relevant = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in (
        "model", "watch_dir", "output_dir", "processed_dir", "failed_dir", "local_fail_dir", "state_dir", "notify")}
    relevant.update(model=model, templates_dir=str(templates_dir.resolve()), publish_year=manifest["publish_year"])
    return relevant


def ensure_isolated_results(run_dir, corpus, templates_dir, cfg):
    blocked = [corpus, templates_dir] + [Path(cfg[key]).resolve() for key in (
        "output_dir", "watch_dir", "processed_dir", "failed_dir", "local_fail_dir", "state_dir"
    ) if cfg.get(key)]
    for path in blocked:
        if run_dir.is_relative_to(path) or path.is_relative_to(run_dir):
            raise ValueError(f"Results overlap an input or configured production directory: {path}")


def read_records(path):
    """Recover a torn final append, but never hide corruption in the middle."""
    if not path.exists():
        return []
    raw = path.read_text()
    lines = raw.splitlines()
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except ValueError:
            if index != len(lines) - 1:
                raise ValueError("Corrupt non-final row in attempts.jsonl")
            atomic_write(path.with_name("interrupted-final-row.txt"), line.encode())
            break
    normalized = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    if normalized != raw:
        atomic_write(path, normalized.encode())
    return records


def pipeline_fingerprint():
    digest = hashlib.sha256()
    for path in [ROOT / "thesis_cover_watch.py", *sorted(ROOT.glob("cover_*.py"))]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


@contextmanager
def single_runner(run_dir):
    with (run_dir / ".runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another runner is already writing this experiment") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_suite(corpus, cfg, *, results_dir=DEFAULT_RESULTS, rounds=3, seed=20261125, order="mixed",
              selected=None, tags=None, resume=None, templates_dir=None):
    corpus = corpus.resolve()
    manifest = load_manifest(corpus)
    templates_dir = Path(templates_dir or manifest["templates_dir"]).resolve()
    cases = {c["id"]: c for c in manifest["cases"]}
    configuration = safe_configuration(cfg, templates_dir, manifest)
    missing = {c["expected"]["university_id"] for c in cases.values() if c["expected"]["decision"] == "stamp"} - {
        u["id"] for u in cfg["universities"]}
    if missing:
        raise ValueError(f"Configured catalog is missing fixture institutions: {sorted(missing)}")
    if not templates_dir.is_dir():
        raise ValueError(f"Template directory is unavailable: {templates_dir}")
    if resume:
        run_dir = resume.resolve()
        ensure_isolated_results(run_dir, corpus, templates_dir, cfg)
        plan = json.loads((run_dir / "plan.json").read_text())
        if (plan["corpus_sha256"] != digest_file(corpus / "manifest.json") or plan["configuration"] != configuration
                or plan.get("pipeline_sha256", pipeline_fingerprint()) != pipeline_fingerprint()):
            raise ValueError("Resume requires the same pipeline, corpus, and effective configuration")
    else:
        ids = [key for key, value in cases.items() if (not selected or key in selected)
               and (not tags or set(tags).intersection(value["tags"]))]
        if not ids or (selected and set(selected) - set(cases)):
            raise ValueError("No matching cases, or an unknown --case ID")
        run_dir = results_dir.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
        ensure_isolated_results(run_dir, corpus, templates_dir, cfg)
        run_dir.mkdir(parents=True)
        plan = {"schema": SCHEMA, "corpus": str(corpus), "corpus_sha256": digest_file(corpus / "manifest.json"),
                "pipeline_sha256": pipeline_fingerprint(), "created_utc": datetime.now(timezone.utc).isoformat(),
                "seed": seed, "order": order, "rounds": rounds, "configuration": configuration,
                "schedule": schedule(ids, rounds, seed, order)}
        atomic_write(run_dir / "plan.json", (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode())
    attempt_ids = [item["attempt"] for item in plan["schedule"]]
    if (len(attempt_ids) != len(set(attempt_ids)) or
            any(not re.fullmatch(r"\d+_r\d+_[a-z0-9_]+", item["attempt"]) or item["case"] not in cases
                for item in plan["schedule"])):
        raise ValueError("Invalid or duplicate attempt identifiers in saved plan")
    with single_runner(run_dir):
        return execute_plan(run_dir, plan, cases, corpus, cfg, templates_dir, manifest)


def execute_plan(run_dir, plan, cases, corpus, cfg, templates_dir, manifest):
    print(f"Results: {run_dir}", flush=True)
    records_path = run_dir / "attempts.jsonl"
    records = read_records(records_path)
    completed = {r["attempt"] for r in records}
    if len(completed) != len(records) or not completed.issubset({item["attempt"] for item in plan["schedule"]}):
        raise ValueError("Attempt records contain duplicates or do not belong to this plan")
    try:
        for item in plan["schedule"]:
            if item["attempt"] in completed:
                continue
            print(f"[{len(records) + 1}/{len(plan['schedule'])}] {item['case']} round {item['round']}", flush=True)
            result = run_attempt(cases[item["case"]], item, corpus, run_dir, cfg, templates_dir, manifest["publish_year"])
            with records_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            records.append(result)
            write_reports(run_dir, records, plan)
            print(f"  {result['verdict']}: {result['seconds']}s; {result['model_calls']} model call(s)", flush=True)
            if len(records) >= 3 and all(r["verdict"] == "infrastructure_error" for r in records[-3:]):
                print("Stopped after three consecutive infrastructure failures; resume after restoring the backend/storage.", flush=True)
                break
    finally:
        summary = write_reports(run_dir, records, plan)
    return run_dir, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Generate PDFs, previews, assets, and ground-truth manifest; no inference")
    build.add_argument("--output", type=Path, default=DEFAULT_CORPUS)
    build.add_argument("--templates-dir", type=Path, default=ROOT / "Prägungen")
    build.add_argument("--catalog", type=Path, default=ROOT / "config.example.json")
    build.add_argument("--overwrite", action="store_true")
    listing = commands.add_parser("list", help="Show cases and their expected decisions")
    listing.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run = commands.add_parser("run", help="Execute real-model attempts in isolated output directories")
    run.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run.add_argument("--config", type=Path, default=ROOT / "config.json")
    run.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    run.add_argument("--templates-dir", type=Path)
    run.add_argument("--rounds", type=int, default=3)
    run.add_argument("--seed", type=int, default=20261125)
    run.add_argument("--order", choices=("mixed", "repeat"), default="mixed")
    run.add_argument("--case", action="append")
    run.add_argument("--tag", action="append")
    run.add_argument("--resume", type=Path)
    report = commands.add_parser("report", help="Regenerate reports from saved results without inference")
    report.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)
    try:
        if args.command == "build":
            cfg = pipeline.load_config(args.catalog)
            manifest = build_corpus(args.output, args.templates_dir, cfg["universities"], overwrite=args.overwrite)
            print(f"Built {len(manifest['cases'])} cases in {args.output.resolve()}")
            return 0
        if args.command == "list":
            for case in load_manifest(args.corpus)["cases"]:
                print(f"{case['id']:30} {case['tier']:10} {case['expected']['decision']:6} {', '.join(case['tags'])}")
            return 0
        if args.command == "report":
            plan = json.loads((args.run_dir / "plan.json").read_text())
            if digest_file(Path(plan["corpus"]) / "manifest.json") != plan["corpus_sha256"]:
                raise ValueError("Corpus ground truth changed since this experiment")
            with single_runner(args.run_dir):
                summary = write_reports(args.run_dir, read_records(args.run_dir / "attempts.jsonl"), plan)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        cfg = pipeline.load_config(args.config)
        run_dir, summary = run_suite(args.corpus, cfg, results_dir=args.results_dir, rounds=args.rounds,
                                     seed=args.seed, order=args.order, selected=args.case, tags=args.tag,
                                     resume=args.resume, templates_dir=args.templates_dir)
        try:
            pipeline.release_benchmark_models(cfg)
        except Exception:
            logging.getLogger(__name__).debug("Benchmark model release failed", exc_info=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Report: {run_dir / 'report.md'}")
        return 0 if summary["passed"] == summary["planned"] else 1
    except KeyboardInterrupt:
        print("Interrupted. Completed attempts are saved; rerun with --resume RESULTS_DIRECTORY.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
