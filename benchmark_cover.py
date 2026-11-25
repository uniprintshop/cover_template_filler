#!/usr/bin/env python3
"""Repeat real inference with temporary outputs; print JSON timing results."""

import argparse
import json
import logging
import statistics
import tempfile
import time
from pathlib import Path

from thesis_cover_watch import load_config, process_pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    results = []
    with tempfile.TemporaryDirectory(prefix="cover-benchmark-") as temporary:
        root = Path(temporary)
        cfg.update(output_dir=str(root / "out"), state_dir=str(root / "state"),
                   failed_dir=str(root / "failed"), local_fail_dir=str(root / "failed"),
                   notify=False, force=True)
        for run in range(args.runs):
            before = set((root / "out").glob("*.json"))
            started = time.monotonic()
            process_pdf(args.pdf, cfg)
            elapsed = time.monotonic() - started
            sidecar, = set((root / "out").glob("*.json")) - before
            data = json.loads(sidecar.read_text())
            calls = data["fields"].get("inference_metrics", [])
            results.append({"run": run + 1, "seconds": round(elapsed, 3),
                            "source": data["text_source"], "model_calls": len(calls),
                            "university": data["fields"]["university_id"],
                            "extraction": data["fields"].get("extraction_timings", {}),
                            "inference": calls})
    print(json.dumps({"runs": results, "median_seconds": round(statistics.median(r["seconds"] for r in results), 3)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
