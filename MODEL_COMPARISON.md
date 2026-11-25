# Small-model comparison — 2026-11-25

Six mixed cases run once per model against the same corpus and pipeline, host `ollama-box:11434`:

`05_logo_only`, `09_logo_only`, `27_name_and_birthplace`, `35_ambiguous_awarders`, `36_awarder_and_partner`, `46_institution_after_abstract`.

One round only — small samples, illustrative not conclusive.

| Model | Params | Correct | Median | Max | Notes |
|---|---|---:|---:|---:|---|
| `ornith-1.5:9b` (baseline) | 9B | **6/6** | 16.6s | 33.1s | All stamps, fields, and the required rejection correct. |
| `lfm2.5:8b` | 8B | 1/6 | 11.4s | 27.3s | Fast but refuses nearly everything (5 safe refusals); only the ambiguous case passed. |
| `bonsai-2:4b` | 4B | 1/6 | 28.9s | 73.2s | Slowest and unreliable; **2 field mismatches** (stamped with wrong fields), hallucinated `rwth-aachen` on a logo-only cover. |
| `lfm3:1.7b` | 1.7B | 2/6 | 7.4s | 17.7s | Fastest; got the partner case and the ambiguity rejection right, but 3 wrong refusals and 1 field mismatch. |

Results: `reliability_runs/results/20261125T134908Z_bf80dd3c` (lfm3),
`20261125T135004Z_5586e819` (bonsai), `20261125T135324Z_a4c8f0e9` (lfm2.5),
`20261125T135453Z_f9dc27fa` (ornith baseline).

Key findings:

- The pipeline's guards prevented every wrong-foil stamp across all models; the
  failures were refusals (annoying, not dangerous) or field mismatches.
- `bonsai-2:4b` is the only model that produced published-but-wrong fields — the
  most dangerous failure mode — and it was also the slowest.
- `lfm3:1.7b` and `lfm2.5:8b` are fast but fail open toward refusal.
- `ornith-1.5:9b` remains the only tested model that handles the full
  extraction and institution-role workload.
