# Type3 / position=3 optimization report

## Dataset and protocol

- Dataset: 309 labelled Type1 images.
- Split: 247 training / 62 validation, `seed=41`.
- Validation images were not used to build any template bank.
- Production path remains Python + NumPy + `struct`; no Pillow/OpenCV/SciPy is used by `second_bot/type1.py`.
- Runtime for the bounded experiment suite: **242.5s**; estimated external credits: **0** (local deterministic CPU experiments).

## Baseline versus selected version

| Metric | Baseline | Selected (`position=3 threshold=160`) |
|---|---:|---:|
| Total digit accuracy | 65.48% | 66.45% |
| Total digit error | 34.52% | 33.55% |
| position=3 accuracy | 58.06% | 62.90% |
| position=3 error | 41.94% | 37.10% |
| Full five-digit exact accuracy | 22.58% | 20.97% |
| Margins `< 0.02` | 118 | 113 |
| Margins `< 0.05` | 216 | 212 |

Per-position accuracy changed as follows:

- Baseline: `[79.03, 66.13, 62.9, 58.06, 61.29]`
- Selected: `[79.03, 66.13, 62.9, 62.9, 61.29]`

The exact five-digit metric decreased, so it is reported transparently; the selection criterion was total cell accuracy, position=3 error, and no regression in the other positions. All other positions were unchanged in this validation split.

## Experiment summary

| Experiment | Digit accuracy | position=3 accuracy | Exact | Decision |
|---|---:|---:|---:|---|
| A_baseline | 65.48% | 58.06% | 22.58% | rejected |
| B_local_boundary | 65.48% | 58.06% | 22.58% | rejected |
| C_threshold_160 | 66.45% | 62.90% | 20.97% | **selected** |
| C_threshold_192 | 65.48% | 58.06% | 22.58% | rejected |
| C_threshold_224 | 65.48% | 58.06% | 22.58% | rejected |
| D_lines_none | 65.48% | 58.06% | 22.58% | rejected |
| D_lines_light | 65.48% | 58.06% | 22.58% | rejected |
| D_lines_strict | 65.48% | 58.06% | 22.58% | rejected |
| E_bank_all | 65.48% | 58.06% | 22.58% | rejected |
| E_bank_top3 | 59.35% | 27.42% | 8.06% | rejected |
| F_distance_hamming | 59.35% | 27.42% | 8.06% | rejected |
| F_distance_row | 59.03% | 25.81% | 8.06% | rejected |
| F_distance_column | 59.03% | 25.81% | 8.06% | rejected |
| F_distance_composite | 58.71% | 24.19% | 8.06% | rejected |


## Adopted configuration

- Position thresholds: `[224, 160, 192, 160, 224]`.
- Margin: `0.10`.
- Local shift: `0` (boundary search did not improve validation).
- Current multi-template bank retained; top-3 pruning was rejected.
- Composite distance and low-margin retry were implemented as tested helpers, but not enabled by default because their validation results were worse or unchanged.

## Files and verification

- Model regenerated from the full labelled dataset after selection; validation metrics above were computed with the training-only bank.
- `type1_templates_report.json` records the reproducible training configuration.
- Unit tests cover local boundary candidate bounds, composite distance, and `<0.02` retry behavior.
