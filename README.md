# Thesis PDF → UPF cover watcher

When a PDF is dropped into `watch_dir`, the script:

1. Reads pages 1–2 (OCR if there is almost no selectable text)
2. Asks your local model for university / author / title / degree / year
3. **Rejects** any author, title or year that is not an exact substring of the cover text (so Qwen cannot “correct” spelling)
4. Picks the `.upf` template from `universities` aliases
5. Fills 2 or 3 text fields in the UPF
6. Writes **two** `.upf` files when possible: the regular cover, and a `TITEL`/`TITLE` sibling template for the same school (title goes in the extra field)
7. Writes a `.json` sidecar to `output_dir`
8. On failure, writes a `*_FAIL.txt` (extracted facts + a short model report) into `failed_dir` instead of failing silently
9. Moves the PDF to `processed_dir` or `failed_dir`

## Setup (CachyOS)

```bash
sudo pacman -S tesseract tesseract-data-deu tesseract-data-eng poppler
cd ~/thesis-cover
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.json`:

- `watch_dir`, `output_dir`, `templates_dir`, `processed_dir`, `failed_dir`
- `model.name` (Ollama tag, e.g. `qwen3:8b`)
- `universities` — one entry per cover template, with aliases

Put cover files in `templates_dir`, filename matching `universities[].template`.

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

Ollama must already be running. To skip the model and use regex only:

```json
"backend": "none"
```

## Field rules

| TextDesignElement count | What gets written |
|---|---|
| 2 | degree text; author + year. Title ignored |
| 3 | degree; title (middle); author + year |

Degree text comes from `degree_labels` in config (`Bachelor-Thesis` / `Master-Thesis` / `Dissertation`), not from free-typed model output.

## University matching

Templates are chosen only from aliases that **already appear on pages 1–2**. The model cannot invent a school that is not on the PDF.

- One matching school → that template.
- Several matching schools → the model may pick **only among those**. If it cannot, the job fails (no UPF).
- None → fail. Add aliases, or put the institution name on the cover.

Author may stand alone (no “vorgelegt von”). The model may point at that span; Python still requires an exact substring of the cover and rejects supervisor-like names (`Prof.`, Erstgutachter, …). Years must appear on the cover; if several do, the model may choose one of them. PDF metadata is not used as a source of author/title.

The sidecar `.json` includes `ok_to_stamp`, `risks`, and `university_matches`. Check it before foil-stamping if any risk is listed.
