# Thesis PDF → UPF cover watcher

When a PDF is dropped into `watch_dir`, the script:

1. Copies the PDF to a stable local snapshot and reads pages 1–2
2. Extracts embedded images, downscales large ones, deduplicates them, and OCRs their text within a time budget
3. Asks your local model for university / author / title / degree / year
4. Validates the chosen institution against the cover and grounds author/title/year in extracted text
5. Fills each explicitly approved `.upf` template; title siblings are optional
6. Publishes complete UPFs and a `.json` sidecar using temporary files and renames
7. Records completion locally, then moves the source PDF to `processed_dir`
8. On a permanent failure, writes a deterministic `*_FAIL.txt` and moves the PDF to `failed_dir`; transient model/storage failures remain queued for retry

Full-page OCR is used when digital text is too short, or as a second read when
the institution remains unresolved. A backend outage never triggers OCR rescue
or another inference just to explain the failure.

## Setup (CachyOS)

Tested on Linux with Python 3.12. The local job lock uses POSIX `flock`.

```bash
sudo pacman -S tesseract tesseract-data-deu tesseract-data-eng poppler
cd ~/thesis-cover
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Edit `config.json`:

Copy `config.example.json` to `config.json` first. Relative paths are resolved
against the configuration file's directory. `watch_dir` and `templates_dir`
must exist; startup only creates output/archive directories.

- `watch_dir`, `output_dir`, `templates_dir`, `processed_dir`, `failed_dir`
- `notify` — desktop popups (via `notify-send`) when a cover starts, is
  created, or fails; set `false` to disable
- `model` — pick the backend and its keys (see "Model backend" below)
- `universities` — one entry per institution, with aliases

Put cover files in `templates_dir`, filename matching `universities[].template`.

For institutions with several approved regular covers, add a `templates` list
containing their exact filenames (including the primary `template`). Every
existing file in that list is produced; title siblings are found separately.
Without a list, only `template` is used. A shared city name never makes another
institution's template a variant. TH Köln and Rheinische Hochschule Köln have
separate entries even though the historical filenames both start with `TH_Köln`.

After renaming or deleting a file, update these references in `config.json`
and restart the watcher to load the new configuration. If the primary file
disappears, another existing approved variant is used. If none remain, the
job fails if no configured template file remains; it does not search for a different
school's logo. An entry with `"template": "", "templates": []` records an
institution whose templates have been removed.

Selection regression checks:

```bash
.venv/bin/python -m unittest discover -s tests
```

Optional but useful: in each UPF change the dummy strings to:

- degree field: `Thesis`
- author line: `author name`
- year line: `year`
- title field (only if the cover has one): `Title`

## Run

Single file (test):

```bash
source .venv/bin/activate
python thesis_cover_watch.py --config config.json --once /path/to/thesis.pdf -v
```

Watch folder:

```bash
python thesis_cover_watch.py --config config.json --scan-existing
```

## Model backend

The `model` block selects the backend with `"backend"`. The default is the
local `"ollama"` backend; no API key is involved.

- `"ollama"` (default) — a local model on the LAN. Set `name`
  (e.g. `ornith-1.5:9b`) and `host` (e.g. `http://ollama-box:11434`);
  optionally `num_ctx`, `keep_alive` (e.g. `"24h"` keeps the model resident
  so each PDF is fast). Ollama must already be running on that host. Point
  `host` at a name (not a bare IP) resolved via an `/etc/hosts` entry, so a
  DHCP lease change is a one-line fix: `ollama-box <ip>` in `/etc/hosts`.
- `"openai"` — any OpenAI-compatible HTTP endpoint (a hosted gateway or a
  local server). Set `name` (the model id), `openai_base`
  (e.g. `https://api.example.com/v1`) and `api_key`. `think: false` turns the
  model's reasoning trace off so the token budget goes to the JSON answer.
  `openai_json_mode` (default `true`) sends a JSON `response_format`.
- `"none"` — disable inference. No university is guessed from regexes alone;
  a queued job waits until a model is configured and the watcher restarted.

## Field rules

| TextDesignElement count | What gets written |
|---|---|
| 2 | degree text; author + year. Title ignored |
| 3 | degree; title (middle); author + year |

Degree text prefers a whitelisted thesis-kind phrase printed on the cover.
Otherwise `degree_labels` supplies the wording for the resolved degree type.

## University matching

The model chooses an institution ID from the configured catalog. Python then
requires configured alias or distinctive filename evidence on pages 1–2;
an unsupported or ungrounded choice is rejected. Only that institution's
explicit `template`/`templates` assignments are used. The matcher validates the
model's choice; it never chooses a different school on its own.

Multi-word aliases match consecutive phrases, allowing spaces, slashes and
hyphens (e.g. `Universität Witten/Herdecke`). Words scattered across different
parts of the cover do not form a university name. Postal-address and birth
lines are excluded. OCR blocks allow clustered, reordered words within a
bounded window; digital text stays strict. Evidence is checked per block/page,
so two pages cannot accidentally combine into one institution name.

Author may stand alone (no “vorgelegt von”). Python grounds the name in the
cover and rejects supervisor-like names (`Prof.`, Erstgutachter, …). Birth years
are excluded from year candidates. If no publishing year is present, the
explicit `publish_year` setting or PDF creation year can supply a flagged
fallback. Review the fixed `publish_year` setting when the calendar changes.
Metadata is not a standalone source for author/title.

The sidecar `.json` includes `ok_to_stamp`, `risks`, and `university_matches`. Check it before foil-stamping if any risk is listed.

Title variants require `write_title_variant: true`, a matching `TITEL`/`TITLE`
sibling template and a cover-grounded title. If the model changes title words
or supplies no title, the title variant is skipped; `variant_errors` explains
why. A supplied title that fails validation also adds `title_not_on_cover`
to `risks`. This does not prevent writing the regular cover.

## Retry and completion behavior

Local journals default to `$XDG_STATE_HOME/thesis-cover` or
`~/.local/state/thesis-cover`; set `state_dir` to override. Keep this directory
on local persistent storage. A journal records prepared fields, published files,
and completion independently of the source's archive move.

- An interrupted sidecar/output write resumes without another model call once
  fields are prepared.
- A completed job whose archive move failed only retries archival. Even if an
  operator has already moved the finished UPFs, it does not regenerate them.
- A changed source file or changed processing configuration is a new job.
- To deliberately regenerate identical completed input, use
  `--once /path/to/thesis.pdf --force`.
- Output names include a short journal-generation suffix so retries retain
  ownership of their filenames and separate jobs cannot claim the same name.
- `--once` generates outputs but does not archive the input; watcher jobs do.

Events and rescans feed one bounded worker. Copy-stability checks happen in
that worker. Transient failures retry after `retry_seconds`, including when
periodic rescanning is disabled. Rescans also recover events missed by a
network share; retain the default interval for normal operation.

## Extraction and scheduling limits

| Setting | Default | Meaning |
|---|---:|---|
| `ocr_image_max_edge` | 1600 | Maximum embedded-image edge; smaller images are never upscaled |
| `ocr_image_limit` | 12 | Maximum OCR attempts on embedded images |
| `ocr_timeout_seconds` | 8 | Maximum time per OCR call, including language fallback |
| `ocr_budget_seconds` | 20 | OCR pass budget; embedded and full-page passes have separate budgets |
| `ocr_render_timeout_seconds` | 15 | Full-page rasterization timeout, also bounded by the pass budget |
| `watch_queue_limit` | 256 | Maximum pending jobs, including delayed retries |
| `retry_seconds` | 60 | Delay before retrying a transient failure |

Identical embedded images are OCR'd once per PDF. German/English OCR falls back
to English if German language data is unavailable. The model input reserves
space for digital fields and image-derived institution text separately.

## Timing and benchmarks

Logs separate extraction from inference and include queue, settling, and staging
times. Sidecars include extraction timings, page/source provenance, and model
request metrics (load, prompt evaluation, generation, token counts, completion
reason). Ollama durations are reported in **nanoseconds**, wall times in seconds.

Run repeated real inferences without publishing to the production output folder:

```bash
.venv/bin/python benchmark_cover.py /path/to/thesis.pdf --config config.json --runs 3
```

The benchmark uses temporary outputs/journals, disables notifications, leaves
the source PDF untouched, and prints JSON results. It still uses the configured
model and reads the configured templates. See `PERFORMANCE.md` for measurements.

For a varied, ground-truthed corpus instead of repeating one PDF, see
[`RELIABILITY.md`](RELIABILITY.md). `reliability_suite.py` generates 46 synthetic
covers, including 18 logo-only cases using real template branding, then scores
real-model outputs and timing in isolated experiment directories.

### Measured reliability (2026-11-25)

With the current safeguards (`ornith-1.5:9b`, one full-corpus round):

- **44/46 correct outcomes**, no incorrect stamps; median 16.2s, p95 ~32s.
- The two remaining cases are safe refusals (a small noisy wordmark and a
  text-free graphic mark).
- Six critical cases (partner logo, ambiguity, name/birthplace, page-two
  footer, …) passed 18/18 attempts across three mixed-order repeats.
- Earlier defects — partner logo chosen over the awarding institution,
  ambiguous awarders stamped, birthplace included in the author name — were
  fixed by prompt, evidence-context, and validation changes; see
  [`RELIABILITY_IMPROVEMENTS.md`](RELIABILITY_IMPROVEMENTS.md) and
  [`RELIABILITY_BASELINE.md`](RELIABILITY_BASELINE.md).

Small-model comparison (six mixed cases, one round each; see
[`MODEL_COMPARISON.md`](MODEL_COMPARISON.md)):

| Model | Correct | Median | Notes |
|---|---:|---:|---|
| `ornith-1.5:9b` | 6/6 | 16.6s | Handles the full workload |
| `mimo-v2.6:9b` | 6/6 | 16.4s | Handles the full workload; tightest latency spread |
| `lfm2.5:8b` | 1/6 | 11.4s | Fast but refuses nearly everything |
| `bonsai-2:4b` | 1/6 | 28.9s | Slowest; published wrong fields (dangerous) |
| `lfm3:1.7b` | 2/6 | 7.4s | Fastest; frequent wrong refusals |

The validation guards prevented every wrong-foil stamp on all models.
`ornith-1.5:9b` and `mimo-v2.6:9b` are the recommended models.

Full-corpus comparison (46 cases, one round each; see
[`MODEL_COMPARISON.md`](MODEL_COMPARISON.md) for details):

| Model | Correct | Median | Wrong stamps |
|---|---:|---:|---|
| `ornith-1.5:9b` | 44/46 | 16.2s | 0 |
| `mimo-v2.6:9b` | 42/46 | 16.0s | 2 field mismatches |
| `gemma4:e4b` | 42/46 | **3.1s** | 1 wrong template + 1 field mismatch |

`gemma4:e4b` is roughly 5× faster than the other two, but stamped a wrong
institution on the logo-second-page case. Since covers are reviewed before
foil-stamping and the watcher is not run unattended, that trade-off is accepted
and `gemma4:e4b` is the configured standard model. For unattended operation,
`ornith-1.5:9b` remains the safest choice.

## Code and dependencies

- `thesis_cover_watch.py`: orchestration, field heuristics, template roles, CLI
- `cover_extract.py`: source-aware extraction and OCR budgets
- `cover_model.py`: API failures and inference diagnostics
- `cover_fields.py`: shared blocking decisions and institution notes
- `cover_jobs.py`: stable snapshots, job locking/journals, atomic publication
- `cover_queue.py`: deduplicated serial worker and retries
- `cover_config.py`: normalized defaults and validation
- `cover_upf.py`: UPF text parsing/encoding

`requirements.txt` uses the tested pins in `constraints.txt`. Update those pins
deliberately and run the regression suite. Virtual environments are local and
not version-controlled. `template_rename_map.json` is historical migration
data, not an active institution catalog.
