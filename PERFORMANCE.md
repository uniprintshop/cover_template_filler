# Processing measurements

Measured on the configured Ollama backend (`ornith-1.5:9b`) using the real
Schachtsiek `Masterarbeit_2026.pdf`. Outputs were isolated from production.
These samples are not a throughput guarantee; shared inference-server load and
prompt-cache reuse substantially affect elapsed time.

## Before the refactor

The original text-only attempt took approximately **7.55s**, but failed to name
the institution. A full-page OCR rescue produced the correct cover in **36.61s**
with **two model calls**. Fixing embedded-image resizing and prompt truncation
then produced a successful **18.80s** sample with one model call.

## Repeated refactored-pipeline benchmark

Command:

```bash
.venv/bin/python benchmark_cover.py /path/to/Masterarbeit_2026.pdf --config config.json --runs 2
```

| Stage | First sample | Cached repeat |
|---|---:|---:|
| Digital extraction | 0.316s | 0.296s |
| Embedded-image OCR | 0.744s | 0.699s |
| Model prompt evaluation | 39.971s | 0.129s |
| Model request wall time | 47.018s | 6.895s |
| Complete job wall time | **48.187s** | **7.958s** |
| Model calls | **1** | **1** |
| Institution | Westfälische Hochschule | Westfälische Hochschule |

Both samples used `digital+images`, produced the correct template, and avoided
full-page OCR rescue. Each model response used 2,779 prompt tokens and generated
70 tokens. The first sample loaded the model in approximately 0.007s: this was
a **prompt-cache/prefill cost**, not a cold weight load.

The median of two samples is 28.073s, but the per-stage measurements are more
informative than that aggregate. The large difference comes from prompt
evaluation; extraction remains about one second.

Stable instructions and the institution catalog now precede changing document
data, and warmup uses the same prefix. Identical repeats demonstrate cache
reuse; a larger, varied corpus is needed to quantify the benefit across
different covers. Do not infer a universal speedup from these two samples.

## Avoided work

- Embedded-image processing can avoid a second full extraction/inference pass.
- Operator failure reports use no inference.
- A model outage does not trigger OCR or another inference for the same attempt.
- A sidecar retry reuses prepared fields and already-published UPFs.
- An archive-move retry reuses the completed job instead of generating numbered
  duplicate covers.
