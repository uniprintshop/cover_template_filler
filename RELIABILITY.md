# Synthetic PDF reliability and timing suite

See [RELIABILITY_IMPROVEMENTS.md](RELIABILITY_IMPROVEMENTS.md) for the subsequent
role/ambiguity fixes, the unchanged-corpus comparison, repeats, and fresh variants.

This suite tests the **actual PDF → extraction/OCR → model → validation → UPF
pipeline** against explicit answers. It does not replace the model with scripted
responses when run from the CLI. Only the unit tests stub inference.
The current production path sends OCR/digital **text** to the model; this suite
does not test Ornith's direct vision pathway or imply that the model lacks vision.

There are **46 cases**: 36 core cases, 9 stress cases, and one symbol-only
capability probe. **18 cases identify the institution only through graphics**;
five cases explicitly require refusal. All student details and thesis text are
fictional. The image wordmarks come from the repository's real UPF templates.

## Build and inspect

Use the normal pinned dependencies, including the explicitly declared PDFium
renderer. No additional PDF-generation library, model download, or web asset
download is needed.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python reliability_suite.py build
.venv/bin/python reliability_suite.py list
```

Default generated files:

```text
reliability_runs/corpus/
  manifest.json             # Expected fields/decisions, layouts, source and PDF hashes
  contact_sheet.png         # Overview of all first pages
  pdfs/                     # 46 actual PDFs, including multi-page and scanned files
  previews/                 # First-page PNG preview per case
  assets/                   # Real source wordmarks and the symbol-only crop
```

These generated files are ignored by Git. The generator and ground truth are
versionable Python source. To rebuild an existing generated corpus:

```bash
.venv/bin/python reliability_suite.py build --overwrite
```

Overwrite requires the generator's marker file and never deletes unrelated
files. The manifest is the authoritative case list. Do not rebuild a corpus
during an experiment: the runner checks its hashes and requires unchanged
ground truth to resume or regenerate reports.

## Coverage

| Cases | Challenge |
|---|---|
| 01–04 | German/English covers, bachelor/master wording, umlauts, punctuation and wrapped titles |
| 05 | University appears only in a page-sized embedded banner above the old 2-megapixel cutoff |
| 06–09 | Actual HSPV, IU, RWTH and RUB wordmarks; university absent from selectable text; different positions |
| 10 | Institution's wordmark appears only on page two |
| 11 | Real wordmark traced into vector paths: no institution text and no extractable image object |
| 12 | Identical embedded logo reused on both pages |
| 13–15 | Small, compressed/grayscale and sideways wordmarks |
| 16 | Text-free Wuppertal graphic mark; tests a capability beyond reading image lettering |
| 17–19 | Birthplace decoy, birth year and a work-start date competing with submission date |
| 20–22 | Postal city without institution, unsupported university, and missing institution: refuse |
| 23 | Misleading PDF author/title metadata; the visible cover must win |
| 24–28 | Standalone candidate, surname-first comma, name particles, birthplace attached to a name and joint authors |
| 29 | Missing year; expect the explicitly fixed 2026 fallback and its risk flag |
| 30–32 | Generic thesis wording, dissertation, and genuinely missing title |
| 33–34 | Other schools in contents; institution only on page three, beyond the supported window |
| 35 | Two equal awarding institutions with no unique approved joint foil: require operator review |
| 36 | Awarder and partner have different real logos; only role/position distinguishes them |
| 37–39 | Flattened scan, skewed grayscale JPEG scan, landscape cover |
| 40 | Many decorative pictures consume the embedded-image OCR budget before the wordmark |
| 41–42 | Long multi-line title; sparse first page before a page-two cover |
| 43–46 | Two-column form, author only on page two, PDF rotation flag, institution after a long page-two abstract |

### What “logo-only” means here

No institution identity is inserted into a hidden text layer, metadata hint or
ground-truth prompt. Selectable cover text contains the typed thesis fields,
while the actual university name/acronym is confined to the real wordmark.
The vector case similarly contains only drawing commands for the mark.

Most institutional logos include lettering: OCR can recover those names. Case
16 removes the lettering and retains the actual graphic mark. The current
text/OCR pipeline may safely refuse it; that is recorded as a **capability gap**,
not falsely counted as a successful identification. A wrong stamped foil is
more serious than a safe refusal, and the report distinguishes them.

The exact author/title/year expectations are authored independently of model
answers. For example, the bilingual `name, birthplace` form expects only the
person's name, not the following city. Joint authors expect both names. The
ambiguous-awarders case intentionally requires a human rather than accepting
an arbitrary one of the two schools.

## Run the real model

First round across every case:

```bash
.venv/bin/python reliability_suite.py run --config config.json --rounds 1
```

Repeated mixed-order reliability/timing test, the default:

```bash
.venv/bin/python reliability_suite.py run --config config.json --rounds 3 --seed 20261125
```

Three rounds mean **138 processing attempts**, with additional model calls
when OCR rescue is needed. Runtime depends on the configured server; failed
cases can take longer because they exercise the fallback. Requests are serial.
The run command exits with status 1 if any case fails or the plan stops early;
that is a measured result, not necessarily a harness malfunction. Offline
report regeneration exits successfully when the reports were written.

Target just logo cases, or select individual IDs:

```bash
.venv/bin/python reliability_suite.py run --tag logo-only --rounds 3
.venv/bin/python reliability_suite.py run --case 05_logo_only --case 11_vector_wordmark --rounds 2
```

`--order mixed` shuffles each round reproducibly and avoids adjacent identical
cases. To measure immediate-repeat cache behavior separately, use
`--order repeat`. The plan records the exact execution order. The runner does
not evict the shared server's model/cache or restart any service.

The backend and full institution catalog come from `--config`. Templates default
to the local source recorded by the generated corpus, not a production output
folder; `--templates-dir` can override that read-only source. The test fixes
`publish_year` to the manifest's 2026 value so the missing-year case is repeatable.
If changing degree-label business rules, update that case's expected label
explicitly before building a new corpus.

## Isolation and restartability

- No watcher is started. The input PDFs are never archived or edited.
- Notifications are disabled.
- Each attempt gets its own output, archive, failure-report and local-state
  directories under the experiment folder. Nothing is published to production.
- Each attempt has separate journal state, so later rounds really rerun
  inference instead of returning a previously completed job.
- Real calls are wrapped only to count and observe them; their responses are
  not replaced. The normal API wrappers, extraction, guards and UPF writer run.
- Results append after every completed attempt and reports update incrementally.
- Three consecutive infrastructure failures stop the run instead of exhausting
  the rest of the corpus against an unavailable server.

Interrupt with Ctrl+C, then continue the saved plan:

```bash
.venv/bin/python reliability_suite.py run --config config.json --resume reliability_runs/results/RUN_DIRECTORY
```

Resume requires the same pipeline source, corpus and effective configuration. Completed attempts
are skipped; partially published attempts can use their normal job journal.
Timings from resumed attempts are excluded from latency statistics. A torn final
JSONL append is preserved separately and recovered; corruption in the middle is
reported rather than silently ignored.

## Reports and scoring

Each experiment contains:

```text
plan.json              # Exact case order, seed, configuration; API keys omitted
attempts.jsonl         # Durable per-attempt fields, outcomes, errors and timings
attempts.csv           # Spreadsheet-friendly overview
summary.json           # Aggregate accuracy, timing, safety and consistency
report.md              # Human-readable report and every failure's details
attempts/              # Individual UPFs, sidecars, journals, reports and logs
```

Regenerate reports without making any model calls:

```bash
.venv/bin/python reliability_suite.py report reliability_runs/results/RUN_DIRECTORY
```

The first full run and focused-repeat findings are summarized in
[`RELIABILITY_BASELINE.md`](RELIABILITY_BASELINE.md).

The report separates:

- **Correct stamp:** expected institution, approved template and exact expected
  fields; produced UPFs are reparsed to check candidate/year text.
- **Correct rejection:** actual institution-validation refusal for a negative
  case. Backend, template, parser and empty-model failures cannot earn this pass.
- **Wrong/unapproved template** and **unsafe acceptance of a required rejection**.
- **Field mismatches**, including wrong author/year and title/degree differences.
- **Safe rejection of a positive case:** an automation gap, still not a pass.
- **Model errors**, **pipeline errors** and **infrastructure errors**. Infrastructure
  failures are excluded from accuracy, with counts kept visible.

Spelling, case, punctuation and diacritics are significant; only whitespace is
normalized. Required fallback-risk flags are checked. Core, stress and capability
results are shown separately, without silently excluding difficult cases.

Initial parsed model answers are also compared **before Python validation,
heuristic repairs and OCR recovery**. For the missing-year case, the model is
expected to return an empty year; Python supplies the configured fallback. This
initial-answer section does not substitute a successful second OCR answer for a
failed first answer. Unparsed/transport failures remain separate outcome counts.

Timing covers per-PDF processing, staging, extraction, actual inference and file
publication. It excludes watcher polling, copy-settling delay and notifications.
Stage and per-round summaries, median, p95, range, request counts and available
Ollama load/prefill/generation metrics are retained. Raw Ollama API durations
are nanoseconds; aggregate stage and wall times are seconds. The embedded-image
pass also runs on PDFs without images, so its aggregate includes near-empty
checks; use per-case metrics for image-heavy processing costs.

## Interpretation

One round is a baseline, **not proof of consistency**. Repeated rounds identify
cases whose selected fields or decisions change, separately from variations in
initial model answers that Python may repair. A safe but consistently wrong
refusal still fails accuracy. The model server is shared; cache residency and
contention are not controlled. Distinct PDF layouts can also produce identical
extracted text, which is expected for a pipeline using OCR before inference.

These are controlled synthetic covers, not a claim to cover every PDF producer,
language, handwriting style or damaged file. The PDF text generator uses Latin
WinAnsi characters, not CJK/RTL shaping. Real branding is included, but real
customer PDFs remain an important next validation corpus.

Run the automated generator/scoring/isolation regressions independently of the
live model:

```bash
env PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```
