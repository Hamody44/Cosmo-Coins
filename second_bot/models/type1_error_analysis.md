# Type1 digit error analysis

## Evaluation protocol

- Dataset: 309 labelled `type1` images from `samples/label.csv`.
- Split: deterministic random holdout with `seed=41`.
- Training: 247 images.
- Validation: 62 previously unseen images, 310 digit positions.
- Matcher: position-aware bank of real templates, normalized Hamming distance.
- Baseline threshold: grayscale `<= 192`, fixed five-cell margin `10%`.

## Baseline result

The validation set contains 201 correct and 109 incorrect digit decisions:

- Digit accuracy: `201 / 310 = 64.84%`.
- Digit error rate: `109 / 310 = 35.16%`.
- Exact five-digit sequence accuracy: `11 / 62 = 17.74%`.

The result is a genuine holdout measurement. The 100% result obtained by evaluating the full bank on all 309 images is training-set performance and is not used as the quality metric.

## Error concentration by position

| Position (0-based) | Errors | Error rate |
|---:|---:|---:|
| 0 | 16 | 25.81% |
| 1 | 21 | 33.87% |
| 2 | 23 | 37.10% |
| 3 | 26 | 41.94% |
| 4 | 23 | 37.10% |

The fourth visual cell (position 3) is the weakest. This supports a geometry/alignment problem in the fixed equal-width split: crossing lines and variable glyph placement occupy different proportions of that cell, so the normalized crop does not consistently represent the same digit shape.

## Main causes

### 1. Ambiguous templates after line/noise contamination

The nearest and second-nearest templates are very close in the wrong cases:

- 39/109 errors have margin `<= 0.01`.
- 63/109 errors have margin `<= 0.02`.
- 93/109 errors have margin `<= 0.05`.
- 102/109 errors have margin `<= 0.10`.

This is the strongest measured signal: most errors are not confident mismatches; the foreground mask contains enough extra line/noise pixels to make two digit classes nearly equivalent under raw Hamming distance.

### 2. Fixed equal-width segmentation is not aligned to the real glyph boxes

The current robust fallback divides the usable width into five equal cells because crossing lines often remove the empty gaps used by projection segmentation. This prevents missed cuts, but it can include parts of neighboring digits or cut through a glyph. The error rate rises from 25.81% in the first cell to 41.94% in position 3.

### 3. Systematic class confusions

The most frequent true-to-predicted confusions are:

| True → predicted | Count |
|---|---:|
| 0 → 4 | 7 |
| 6 → 3 | 5 |
| 0 → 6 | 5 |
| 9 → 6 | 5 |
| 9 → 7 | 4 |
| 3 → 6 | 3 |
| 0 → 7 | 3 |
| 1 → 8 | 3 |
| 5 → 3 | 3 |
| 7 → 3 | 3 |
| 4 → 2 | 3 |
| 0 → 1 | 3 |
| 8 → 1 | 3 |
| 0 → 8 | 3 |
| 5 → 6 | 3 |

The recurring `0/4/6/9`, `3/6`, and `1/8` pairs indicate that raw pixel Hamming distance is sensitive to missing loops and line strokes. These pairs should receive class-specific features or line removal before matching.

### 4. Threshold sensitivity

With a separately trained bank at each threshold, threshold `224` is slightly better than the current `192` on this holdout, but it does not solve the main issue:

| Threshold | Digit accuracy | Errors |
|---:|---:|---:|
| 128 | 59.68% | 125 |
| 160 | 63.23% | 114 |
| 192 | 64.84% | 109 |
| 224 | 65.81% | 106 |

The modest 0.97 percentage-point gain from 192 to 224 shows that threshold selection is a secondary factor; alignment and contamination are the dominant problems.

## Recommended next improvements

1. Replace equal-width crops with a bounded local search around each nominal cell boundary, scoring candidate boundaries by template distance and projection cut cost.
2. Remove long straight lines with NumPy-only morphology or row/column run suppression before template matching.
3. Keep multiple templates per class, but add a robust feature distance using row/column projections and a small downsampled shape descriptor in addition to Hamming distance.
4. Add an abstention rule for margins below `0.02` instead of returning a low-confidence digit.
5. Use position-specific thresholds or template banks, especially for positions 2–4.
