# Small-model comparison — 2026-11-25

Six mixed cases run once per model against the same corpus and pipeline, host `ollama-box:11434`:

`05_logo_only`, `09_logo_only`, `27_name_and_birthplace`, `35_ambiguous_awarders`, `36_awarder_and_partner`, `46_institution_after_abstract`.

One round only — small samples, illustrative not conclusive.

| Model | Params | Correct | Median | Max | Notes |
|---|---|---:|---:|---:|---|
| `ornith-1.5:9b` (baseline) | 9B | **6/6** | 16.6s | 33.1s | All stamps, fields, and the required rejection correct. |
| `mimo-v2.6:9b` | 9B | **6/6** | 16.4s | 19.6s | All stamps, fields, and the required rejection correct; tightest latency spread (15.3–19.6s). |
| `lfm2.5:8b` | 8B | 1/6 | 11.4s | 27.3s | Fast but refuses nearly everything (5 safe refusals); only the ambiguous case passed. |
| `bonsai-2:4b` | 4B | 1/6 | 28.9s | 73.2s | Slowest and unreliable; **2 field mismatches** (stamped with wrong fields), hallucinated `rwth-aachen` on a logo-only cover. |
| `lfm3:1.7b` | 1.7B | 2/6 | 7.4s | 17.7s | Fastest; got the partner case and the ambiguity rejection right, but 3 wrong refusals and 1 field mismatch. |

Results: `reliability_runs/results/20261125T134908Z_bf80dd3c` (lfm3),
`20261125T135004Z_5586e819` (bonsai), `20261125T135324Z_a4c8f0e9` (lfm2.5),
`20261125T135453Z_f9dc27fa` (ornith baseline), `20261125T150524Z_1e7f1a0d` (mimo-v2.6).

Key findings:

- The pipeline's guards prevented every wrong-foil stamp across all models; the
  failures were refusals (annoying, not dangerous) or field mismatches.
- `bonsai-2:4b` is the only model that produced published-but-wrong fields — the
  most dangerous failure mode — and it was also the slowest.
- `lfm3:1.7b` and `lfm2.5:8b` are fast but fail open toward refusal.
- `ornith-1.5:9b` and `mimo-v2.6:9b` are the only tested models that handle the
  full extraction and institution-role workload; `mimo-v2.6:9b` is the
  more consistent of the two on latency.

## Full-corpus comparison (46 cases, one round each)

| Model | Correct | Median | p95 | Wrong stamps | Failures |
|---|---:|---:|---:|---:|---|
| `ornith-1.5:9b` | 44/46 | 16.2s | ~32s | 0 | 2 safe refusals (small wordmark, symbol-only) |
| `mimo-v2.6:9b` | 42/46 | 16.0s | 33.3s | 2 field mismatches | wrong degree (`30_generic_degree`), wrong title on rotated page (`45`); same 2 safe refusals |
| `gemma4:e4b` | 42/46 | **3.1s** | 7.1s | 1 wrong template + 1 field mismatch | stamped `bergische-ihk` instead of `wuppertal` (`10_logo_second_page`); same rotated-page title miss; same 2 safe refusals |
| `gemma4:12b` | 43/46 | 14.0s | 27.5s | **0** | 1 field mismatch (`32_no_title`); same 2 safe refusals. Zero wrong stamps — ornith-level safety |
| `nemotron-3.5-bolt:e4b` | 29/46 | 9.0s | 13.4s | **14 wrong templates** | systematic confusion: stamped `bergische-ihk` on 14 university covers incl. critical cases (`27`, `46`, `10`); same rotated-page title miss; same 2 safe refusals |

Results: `20261125T125946Z_2e589288` (ornith),
`20261125T150855Z_83d010c3` (mimo-v2.6), `20261125T154616Z_1bb7461e` (gemma4:e4b),
`20261126T081603Z_311b6719` (nemotron-3.5-bolt), `20261126T084829Z_831b1296` (gemma4:12b).

Assessment: `gemma4:e4b` is ~5× faster but published a wrong institution foil
once. Since covers are reviewed before stamping and the watcher is not run
unattended, `gemma4:e4b` is the configured standard model; `ornith-1.5:9b`
remains the choice for unattended operation. `nemotron-3.5-bolt:e4b` is
unusable: it systematically applies the Bergische IHK template to university
covers, producing constant false work.
