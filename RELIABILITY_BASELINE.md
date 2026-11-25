# First synthetic-cover baseline

Measured on 2026-11-25 with the configured `ornith-1.5:9b` Ollama backend,
`think: false`, a 16,384-token context and the full 168-institution catalog.
The production pipeline modules were those at commit `8669d18`.

The suite uses the current **OCR/digital-text request path**, not direct model
vision. These results do not imply that Ornith lacks vision support.

## Full mixed-order round

Experiment: `reliability_runs/results/20261125T114722Z_a806e3f2/`.

| Measure | Result |
|---|---:|
| Cases attempted/scored | 46 / 46 |
| Full expected outcomes | **40 / 46** |
| Core cases | 35 / 36 |
| Stress cases | 5 / 9 |
| Symbol-only capability probe | 0 / 1 |
| Logo-only cases, including stress/probe | 15 / 18 |
| Correct stamps | 36 |
| Correct refusals | 4 |
| Required refusal incorrectly stamped | **1** |
| Wrong university/template selected | **1** |
| Wrong candidate content | **1** |
| Safe refusals of positive cases | 3 |
| Infrastructure/model-response errors | 0 |
| Actual model calls | 58 |
| Observed processing time, summed | 889.251s (14m 49s) |
| Median processing time | **15.499s** |
| p95 processing time | **31.325s** |
| Slowest case | **41.777s** |

This is a deliberately structured synthetic corpus, not an estimate of customer
failure prevalence. The wrong-stamping outcomes matter more than the aggregate
pass percentage. One full-corpus round alone does not establish consistency.

## Focused consistency check

Five selected cases were then run in three mixed-order rounds, with fresh
journals per attempt: **15 additional attempts and 21 model calls**.
Experiment: `reliability_runs/results/20261125T120536Z_343eb611/`.

| Case | Same outcome in all three focused rounds |
|---|---|
| `05_logo_only` | Correct Westfälische Hochschule cover |
| `13_small_wordmark` | Safe refusal despite a readable HSPV wordmark |
| `27_name_and_birthplace` | Incorrectly included birthplace in the author |
| `35_ambiguous_awarders` | Stamped instead of requiring review |
| `36_awarder_and_partner` | Chose the wrong institution |

There was no observed decision/field variation among these repeated cases,
including in their initial parsed model answers. The defects are reproducible
in this sample, not merely isolated random outputs. A consistently wrong answer
is still wrong.

The selected repeat set intentionally overrepresents failures: its 3/15 pass
count must not be generalized as an overall accuracy estimate. Median time was
**16.704s**, p95/maximum **34.212s**. Together, the full round and focused repeats
made **61 real processing attempts and 79 real model requests**, totaling
1,228.573s (about 20m 29s) of observed per-case processing. Report generation and
time between experiments are outside that sum.

Reproduction command:

```bash
.venv/bin/python reliability_suite.py run --rounds 3 \
  --case 05_logo_only --case 13_small_wordmark --case 27_name_and_birthplace \
  --case 35_ambiguous_awarders --case 36_awarder_and_partner
```

## What failed

| Case | Expected | Observed |
|---|---|---|
| `27_name_and_birthplace` | Candidate `Nora Seidel` | Stamped `Nora Seidel, Hagen`, including birthplace |
| `13_small_wordmark` | HSPV identified from the reduced-resolution wordmark | Safely refused after two model calls |
| `46_institution_after_abstract` | Wuppertal identified from a page-two footer | Safely refused; longest case at 41.777s |
| `35_ambiguous_awarders` | Operator review for two equal awarders | Produced Bremen's foil |
| `16_symbol_only_probe` | Identify the real Wuppertal graphic mark | Safely refused by the text/OCR path |
| `36_awarder_and_partner` | Westfälische Hochschule, the upper awarding logo | Produced the lower partner's HSPV foil |

The HSPV log is especially informative: embedded-image OCR produced
`HSPVNR\W`. The first model correctly proposed `hspv-nrw`, but the grounding
guard rejected the noisy acronym; full-page OCR then failed to recover it.
This is not simply a failure to recognize the right school in the initial model.

The partner case first produced an ungrounded Wuppertal guess, which was
correctly vetoed. The OCR rescue then chose the partner HSPV; because its name
really appears on the cover, the presence-only guard accepted it. Page/source
provenance does not preserve enough spatial/role association for this case.

## What worked

- Real raster wordmarks for WH, HSPV, IU, RWTH, RUB and Wuppertal.
- A >2-megapixel page-sized banner, bottom-positioned logo, page-two logos,
  duplicate images, compressed/grayscale branding, landscape and PDF rotation.
- Vector-only and sideways wordmarks, recovered through full-page OCR.
- Full-page and mildly skewed scans.
- Name particles, surname-first names, joint authors and two-column forms.
- Birth-year/start-date decoys, hostile metadata, contents mentioning other
  schools, dissertation wording and a long punctuated title.
- Refusal of missing/unsupported schools, an address-only city and an
  institution occurring only on page three.

## Timing interpretation

Most cases needed one model request. Twelve needed a second OCR/model pass,
bringing the total to 58. The median inference-request time per case was about
14.9s; prompt evaluation accounted for about 10.4s and generation about 4.4s.
These are medians over individual per-case sums, not additive stage guarantees.

The initial parsed model responses got all scored fields right in 36/46 cases;
institution IDs were right in 37/46. Candidate names were right in 40/41
positive cases, and titles, years and degree types in 41/41. Python validation
and OCR recovery changed some initial outcomes, which is why the final pipeline
score differs. Missing-year raw answers were expected to be empty, with Python
applying the configured fallback.

## Priority indicated by these tests

1. Resolve awarding-institution roles and detect genuine multi-awarder ambiguity
   before accepting a merely present institution name.
2. Separate candidate name from birthplace in explicitly combined form fields,
   without damaging ordinary surname-first comma names.
3. Improve bounded prompt selection so page-two/footer institution evidence
   survives, including in the fallback.
4. Investigate conservative OCR-acronym normalization using the small-wordmark
   regression, rather than broadly relaxing the university veto.
5. Treat direct vision as a separately measured option for text-free marks and
   spatial role interpretation, not as a capability disproved by this text path.

Detailed artifacts, exact answers, model metrics and generated UPFs are kept in
the experiment directory. See `RELIABILITY.md` for rebuilding, repeating and
regenerating reports. No production watcher, model setting or validation rule
was changed to make these fixtures pass.
