# Adaptive per-image/per-position threshold experiment

## Protocol

- Dataset: 309 labelled Type1 images.
- Split: 247 training / 62 validation, `seed=41`.
- Validation images were not used to build template banks.
- Threshold candidates per position: `160, 176, 192, 208, 224`.
- Candidate limit: 3 digits per position.
- Beam width: 10.
- Production constraints: Python standard library + NumPy + `struct`; Pillow is used only by the offline experiment loader.
- Estimated external credits: 0; local runtime: approximately 27.3 seconds.

## Results

| Metric | Fixed production baseline | Adaptive thresholds + Beam Search |
|---|---:|---:|
| Exact five-digit accuracy | **22.58%** | 20.97% |
| Total digit accuracy | 65.48% | **66.13%** |
| Position 0 accuracy | 79.03% | 77.42% |
| Position 1 accuracy | 66.13% | **70.97%** |
| Position 2 accuracy | 62.90% | 59.68% |
| Position 3 accuracy | 58.06% | **61.29%** |
| Position 4 accuracy | 61.29% | 61.29% |

## Decision

The adaptive method was **not adopted**. It improves average cell accuracy and position 3, but it creates errors in other positions and loses one exact five-digit validation sample (`14/62` to `13/62`). The production model remains the exact-optimized fixed configuration:

```text
[224, 160, 192, 192, 224]
```

The experiment code is retained for future work. A stronger adaptive method would need confidence calibration learned on an inner training split, joint boundary quality features, and a sequence-level score that is not merely the sum of independent Hamming distances.
