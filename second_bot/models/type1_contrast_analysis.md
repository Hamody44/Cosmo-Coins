# Type1 residual error and contrast normalization analysis

## Evaluation protocol

- 247 training images and 62 untouched validation images.
- 310 validation digit positions.
- Current position thresholds: `[224, 160, 192, 192, 224]`.
- Local Shift is available but the inner tuning selected `0`; forcing shifts reduced accuracy.

## Current residual error

The current model recognized 203/310 validation digits and missed 107/310:

- Digit accuracy: **65.48%**.
- Residual digit error: **34.52%**.
- Position errors: `[13, 21, 23, 26, 24]` for positions 0–4.
- Position error rates: `20.97%, 33.87%, 37.10%, 41.94%, 38.71%`.

The fourth cell (position 3) remains the weakest. This is consistent with fixed-cell geometry and crossing lines affecting the crop, not with a single global brightness problem.

## Dominant root causes

1. **Crop alignment and cell geometry:** errors increase from 20.97% in the first cell to 41.94% in position 3. A fixed equal-width crop can include neighboring strokes or cut through the glyph.
2. **Line contamination:** the most common confusions are `0→6`, `9→6`, `0→4`, `7→3`, `0→8`, and `1→8`. These pairs are consistent with loops being opened/filled by crossing lines.
3. **Template ambiguity:** the nearest-template decision is often inherently weak. The median top-two margin over all decisions was about `0.030`; the most common error cases had very small margins, so raw Hamming distance cannot separate the classes reliably.
4. **Class imbalance by position:** some digit/position combinations are rare in the training subset, so a position-specific bank has fewer visual examples for those classes.

## Contrast normalization experiment

Each candidate was retrained on the training subset and evaluated on the same untouched validation subset. The baseline uses the current position thresholds.

| Variant | Digit accuracy | Errors |
|---|---:|---:|
| Raw grayscale + current thresholds | **65.48%** | 107 |
| Global percentile stretch 2–98 | **65.48%** | 107 |
| Per-cell percentile stretch 2–98 | **65.48%** | 107 |
| Global percentile stretch 5–95 | 65.16% | 108 |
| Global z-score-like stretch | 64.84% | 109 |
| Per-cell stretch 10–90 + threshold 128 | 59.35% | 126 |

### Interpretation

Global percentile stretching produced an identical binary decision pattern in this dataset. This is expected: percentile stretching is monotonic, so with a correspondingly fixed threshold it does not remove the crossing lines or repair crop boundaries. Aggressive per-cell stretching amplified background and line pixels and reduced accuracy by 6.13 percentage points.

Therefore contrast normalization is implemented as an **opt-in NumPy function**, but it is not enabled in the production path because the holdout experiment did not improve recognition. The next high-value changes are line suppression, local boundary selection constrained by glyph geometry, and a richer distance combining Hamming with row/column projections.
