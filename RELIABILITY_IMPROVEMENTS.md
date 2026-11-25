# Institution-role and author-field improvements — 2026-11-25

The original [baseline](RELIABILITY_BASELINE.md) and its 46 PDF inputs/answers
are preserved. These results use the same real Ornith backend and isolated
publication paths. The live watcher was not restarted.

## Changes

- Prompt explicitly distinguishes awarding institutions from cooperation
  partners, employers and supervisors' affiliations. A single university
  letterhead remains sufficient; an explicit "awarded by" label is not required.
- Model returns `institution_status`: `identified`, `ambiguous`, or `not_found`.
  Explicit ambiguity blocks publication and is not overridden by an OCR retry.
  Legacy responses without this field retain existing validation behavior.
- Embedded-logo OCR includes page and upper/middle/lower image-position labels
  derived from PDF image coordinates. Unknown/conflicting positions stay unknown.
- The bounded 2,400-character evidence window allocates space across pages and
  sources and retains both starts and ends of oversized blocks. This preserves
  institutional footers after long abstracts.
- Explicit German/English joint-awarding language independently vetoes a
  single-institution foil, even when the model confidently chooses one.
- A city-only grounding match is vetoed if a different configured institution
  is explicitly named in the same evidence. This is a veto, not automatic
  selection of the other school; the model still selects the institution.
- A combined name/birthplace field's label permits removal of the final
  comma-separated birthplace. Unlabelled surname-first commas are preserved.
- The scorer recognizes `university_ambiguous` as a deliberate refusal.
  Expected decisions/fields have not been relaxed.

## Final full-corpus comparison

| Measurement | Original baseline | Revised pipeline |
|---|---:|---:|
| Correct final outcomes | 40/46 | **44/46** |
| Correct stamps | 36 | **39** |
| Correct refusals | 4 | **5** |
| Incorrect stamps: author / required refusal / wrong school | 1 / 1 / 1 | **0 / 0 / 0** |
| Safe refusals of expected positives | 3 | **2** |
| Logo-only outcomes correct | 15/18 | **16/18** |
| Median processing | 15.499 s | **16.203 s** |
| p95 processing | 31.325 s | **32.998 s** |
| Actual model calls | 58 | **57** |

Final full run: `reliability_runs/results/20261125T125946Z_2e589288/`.
Total measured processing: 910.201 seconds. Timing differences also reflect
backend/cache conditions and should not be interpreted as a controlled speed
comparison. There were no model or infrastructure errors.

The two remaining failures safely refused the noisy small HSPV wordmark and
the text-free Wuppertal symbol. No permissive acronym matching or direct-vision
fallback was added to force them through.

## Repeatability and fresh variations

Six cases (large banner control, RUB logo/shared city, combined birthplace,
joint awarders, awarder versus partner, page-two abstract/footer) were run in
three mixed-order rounds: **18/18 correct final outcomes**, with no variation.
The model still initially chose the wrong Bochum institution and retained the
birthplace; validation/OCR recovery and contextual name correction mattered.
Prompt changes alone have not solved those cases.

Repeat run: `reliability_runs/results/20261125T131505Z_0a10ffa6/`.
21 real model calls; median 16.800 s, p95 32.874 s.

Four additional PDFs were then generated separately, without changing the
pipeline after seeing their answers:

| Fresh variation | Outcome |
|---|---|
| Different universities, lower logo awards and upper logo is partner | Safe refusal; model reported ambiguity |
| Different universities with English joint-awarding wording | Correct refusal |
| Surname-first author plus birthplace under combined label | Correct stamp; surname comma preserved |
| Same surname-first author without birthplace label | Correct stamp; comma preserved |

Thus **3/4 fresh variations passed**, with no incorrect publication. The
reversed-logo refusal is an important remaining limitation: the original
partner fixture passing does not establish general visual role understanding.

Fresh run: `reliability_runs/results/20261125T132225Z_25d4492f/`.

Reproduce those extra inputs and their live test:

```bash
.venv/bin/python reliability_holdouts.py --config config.json
.venv/bin/python reliability_suite.py run --config config.json \
  --corpus reliability_runs/holdouts --rounds 1
```

Use `--overwrite` on the generator to rebuild an existing marked holdout corpus.

## Intermediate experiments

These were iterative combined prompt/evidence changes, not a prompt-only
causal experiment:

- `20261125T123623Z_411ba709`: the first prompt was over-cautious and refused
  valid single-school covers. Its report shows 37/46 and one `pipeline_error`:
  the scorer did not yet recognize the newly introduced ambiguity risk. The
  underlying joint-awarder attempt deliberately refused; the saved report is
  retained as produced rather than rewritten.
- `20261125T125539Z_a800be70`: the relaxed prompt recovered valid covers but
  exposed an unsafe joint-awarder choice and city-only Bochum confusion.
  These motivated the explicit joint-award and conflicting-name vetoes.

## Interpretation

The final pipeline is materially better on the measured cases, particularly
at preventing incorrect publication. These are synthetic inputs, with one full
round and focused repeats, not a measured customer-PDF error rate. Review of
actual customer covers remains appropriate; unresolved logo roles can still
require manual handling.
