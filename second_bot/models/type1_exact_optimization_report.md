# Exact-five-digit Type1 optimization report

## Objective and protocol

The primary metric in this iteration is exact recognition of all five digits. The split remains `247 training / 62 validation`, `seed=41`; validation images were not used to build banks or choose internal parameters. The bounded suite took **164.9s** and used no external credits.

## Final result

| Metric | Previous committed version (`p3=160`) | Exact-optimized version (`p3=192`) |
|---|---:|---:|
| Exact five-digit accuracy | 20.97% | **22.58%** |
| Exact five-digit errors | 79.03% | 77.42% |
| Total digit accuracy | 66.45% | 65.48% |
| `position=3` accuracy | 62.90% | 58.06% |
| `position=3` error | 37.10% | 41.94% |
| Margins `<0.02` | 113 | 118 |
| Margins `<0.05` | 212 | 216 |

The final choice intentionally trades some per-cell and position-3 accuracy for a higher full-number accuracy, because exact five-digit accuracy was the requested objective.

## Applied and tested suggestions

| Technique | Result | Decision |
|---|---|---|
| Beam Search with same candidates | 20.97% exact | Implemented in production API; no gain when scores are additive |
| Threshold ensemble | 20.97% exact | Rejected; no gain |
| Threshold ensemble with retry `<0.02` | 20.97% exact | Rejected; no gain |
| Threshold ensemble with retry `<0.05` | 20.97% exact | Rejected; no gain |
| Shift ensemble | 14.52% exact | Rejected; regression |
| All-path ensemble | 14.52% exact | Rejected; regression |
| Top-5 template candidates | 14.52% exact | Rejected; regression |
| Exact-aware threshold `p3=192` | **22.58% exact** | **Selected** |
| Boundary search | No improvement in previous bounded validation | Rejected for production |
| Line removal | No measurable improvement in previous bounded validation | Rejected for production |
| Composite distance | Worse than Hamming in previous bounded validation | Rejected for production |
| Low-margin retry | No improvement; some variants regressed | Kept as tested helper, not enabled by default |

## Error analysis

For the previous `p3=160` validation run, the number of wrong positions per image was recorded in the JSON report. This confirms that the exact metric is sensitive to errors distributed across multiple positions, so changing one position's cell accuracy does not necessarily improve full-string accuracy.

## Production changes

- Added `rank_position_candidates` for bounded per-position candidate lists.
- Added `beam_decode` with configurable beam width.
- Added optional `beam_width` and `candidate_limit` to `recognize_bmp`; default behavior remains unchanged.
- Added unit tests for Beam Search and candidate ranking.
- Regenerated `type1_templates.npz` with thresholds `[224, 160, 192, 192, 224]`.

## Files

- `second_bot/models/type1_exact_optimization_report.json`
- `second_bot/tools/optimize_type1_exact.py`
- `second_bot/models/type1_templates.npz`
