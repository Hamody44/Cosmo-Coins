"""Type1 OCR segmentation and compact shift-tolerant feature extraction."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps

IMAGE_SIZE = (300, 110)
CANVAS_SIZE = (28, 28)
CONTENT_SIZE = (24, 24)  # two-pixel margin
BASE_FEATURE_LENGTH = 14 * 14 + 20 + 20 + 16 + 3
GRADIENT_FEATURE_LENGTH = 4 * 6 * 6
TOPOLOGICAL_FEATURE_LENGTH = 16
FEATURE_LENGTH = BASE_FEATURE_LENGTH + GRADIENT_FEATURE_LENGTH + TOPOLOGICAL_FEATURE_LENGTH


def box_sum_7x7(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), ((3, 3), (3, 3)), constant_values=0)
    integral = np.pad(padded, ((1, 0), (1, 0)), constant_values=0).cumsum(0).cumsum(1)
    return integral[7:, 7:] - integral[:-7, 7:] - integral[7:, :-7] + integral[:-7, :-7]


def _line_candidates_half(mask: np.ndarray) -> np.ndarray:
    """Find long line pixels on a 150x55 mask using the fixed 18-angle sweep."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 8:
        return np.zeros_like(mask, dtype=bool)
    xf, yf = xs.astype(np.float32), ys.astype(np.float32)
    candidates = np.zeros(len(xs), dtype=bool)
    for angle in range(0, 180, 10):
        theta = np.deg2rad(angle); ct, st = float(np.cos(theta)), float(np.sin(theta))
        rho = xf * ct + yf * st; bins = np.rint(rho).astype(np.int32)
        b = bins - int(bins.min()); counts = np.bincount(b); active = np.flatnonzero(counts >= 8)
        if not len(active): continue
        if len(active) > 2: active = active[np.argsort(counts[active])[-2:]]
        t = -xf * st + yf * ct
        for bin_index in active:
            selected = b == bin_index; values = t[selected]
            if float(values.max() - values.min()) < 40.0: continue
            center = float(np.median(rho[selected])); t0, t1 = float(values.min()) - 1.0, float(values.max()) + 1.0
            offsets = np.linspace(-1.0, 1.0, 9, dtype=np.float32)[:, None]
            near = ((np.abs(rho[None, :] - center - offsets) <= 0.75) & (t[None, :] >= t0) & (t[None, :] <= t1))
            candidates |= near.any(axis=0)
    result = np.zeros_like(mask, dtype=bool); result[ys[candidates], xs[candidates]] = True
    return result


def remove_long_straight_lines(mask: np.ndarray) -> np.ndarray:
    """Detect lines at half resolution, then apply the full-resolution density guard."""
    mask = np.asarray(mask, dtype=bool)
    small_image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L").resize((150, 55), Image.Resampling.NEAREST)
    small_candidates = _line_candidates_half(np.asarray(small_image, dtype=np.uint8) > 0)
    full_candidates = np.asarray(Image.fromarray((small_candidates.astype(np.uint8) * 255), mode="L").resize(mask.shape[::-1], Image.Resampling.BILINEAR), dtype=np.uint8) >= 127
    ys, xs = np.nonzero(mask)
    if not len(xs): return mask.copy()
    density = box_sum_7x7(mask) / 49.0
    result = mask.copy(); remove = full_candidates[ys, xs] & (density[ys, xs] < 0.72)
    result[ys[remove], xs[remove]] = False
    return result


remove_long_lines = remove_long_straight_lines


def _column_projection(mask: np.ndarray) -> np.ndarray:
    h = mask.shape[0]
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    return mask[y0:y1].sum(axis=0).astype(np.float32)


def find_dividers(mask: np.ndarray) -> list[int]:
    """Globally optimize four cuts for five 24..68px slots by vectorized DP."""
    h, w = mask.shape
    projection = _column_projection(mask)
    lower, upper = int(np.ceil(0.04 * w)), int(np.floor(0.96 * w))
    cut_cost = np.zeros(w + 1, dtype=np.float32)
    cut_cost[1:w] = (projection[:-1] + projection[1:]) / max(1.0, 2.0 * h)
    dp = np.full((5, w + 1), np.inf, dtype=np.float64)
    previous = np.full((5, w + 1), -1, dtype=np.int16)
    dp[1, 24:min(68, upper) + 1] = cut_cost[24:min(68, upper) + 1]
    starts = np.arange(w + 1)[:, None]
    ends = np.arange(w + 1)[None, :]
    slot_valid = (ends - starts >= 24) & (ends - starts <= 68)
    for completed in range(2, 5):
        valid = slot_valid.copy()
        if completed == 2:
            valid &= starts >= lower
        valid &= (ends >= lower) & (ends <= upper)
        values = dp[completed - 1, :, None] + cut_cost[None, :]
        values = np.where(valid, values, np.inf)
        previous[completed] = np.argmin(values, axis=0).astype(np.int16)
        dp[completed] = values.min(axis=0)
    choices = [(dp[4, c], c) for c in range(lower, upper + 1)
               if 24 <= w - c <= 68 and np.isfinite(dp[4, c])]
    if not choices:
        return [round(w * i / 5) for i in range(1, 5)]
    _, last = min(choices, key=lambda item: (item[0], abs(item[1] - 0.8 * w)))
    cuts = [last]
    for completed in range(4, 1, -1):
        cuts.append(int(previous[completed, cuts[-1]]))
    return sorted(cuts)


def tight_digit_crop(mask: np.ndarray, x0: int, x1: int) -> np.ndarray:
    """Tight crop using row-span continuity; never hard-clips tall glyphs."""
    h, _ = mask.shape; slot = np.asarray(mask[:, x0:x1], dtype=bool)
    if not slot.any(): return np.full((1, 1), 255, dtype=np.uint8)
    row_active = slot.any(axis=1); active_rows = np.flatnonzero(row_active)
    if not len(active_rows): return np.full((1, 1), 255, dtype=np.uint8)
    # Merge row runs separated by at most two empty rows, then select the run
    # intersecting the central anchor band. Detached circles separated by >=3
    # empty rows therefore disappear without removing tall digit strokes.
    gaps = np.flatnonzero(np.diff(active_rows) >= 4) + 1
    runs = np.split(active_rows, gaps)
    anchor_top, anchor_bottom = int(round(0.25 * h)), int(round(0.75 * h))
    overlapping = [run for run in runs if run[0] < anchor_bottom and run[-1] + 1 > anchor_top]
    chosen = max(overlapping or runs, key=len)
    top, bottom = int(chosen[0]), int(chosen[-1]) + 1
    kept = slot[top:bottom]
    ys, xs = np.nonzero(kept)
    if not len(xs): return np.full((1, 1), 255, dtype=np.uint8)
    left, right = max(0, int(xs.min()) - 2), min(slot.shape[1], int(xs.max()) + 3)
    top_pad, bottom_pad = max(0, top - 2), min(h, bottom + 2)
    return np.where(slot[top_pad:bottom_pad, left:right], 0, 255).astype(np.uint8)


def segment_type1_image(image: Image.Image) -> list[Image.Image]:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.size != IMAGE_SIZE:
        raise ValueError(f"Type1 segmentation requires {IMAGE_SIZE[0]}x{IMAGE_SIZE[1]}, got {image.size}")
    cleaned = remove_long_straight_lines(np.mean(np.asarray(image, dtype=np.uint8), axis=2) < 115)
    cuts = [0, *find_dividers(cleaned), cleaned.shape[1]]
    return [Image.fromarray(tight_digit_crop(cleaned, cuts[i], cuts[i + 1]), mode="L") for i in range(5)]


def _line_features(pixels: np.ndarray) -> np.ndarray:
    """Return 16 bounded topology proxies without graph traversal."""
    binary = pixels >= 0.12
    values: list[float] = [
        float(pixels[10:18, 10:18].mean()),
        float(pixels[12:16, 12:16].mean()),
        float(pixels[4:13, 3:12].mean()),
        float(pixels[15:24, 3:12].mean()),
    ]
    for y in (8, 14, 20):
        line = binary[y, :]
        values.append(float(np.count_nonzero(line[1:] != line[:-1])) / max(1, line.size - 1))
    for x in (10, 14, 18):
        line = binary[:, x]
        values.append(float(np.count_nonzero(line[1:] != line[:-1])) / max(1, line.size - 1))
    # Six line-density companions make the topology cues robust to antialiasing.
    for y in (8, 14, 20):
        values.append(float(pixels[y, 3:25].mean()))
    for x in (10, 14, 18):
        values.append(float(pixels[3:25, x].mean()))
    return np.asarray(values, dtype=np.float32)


def prepare_crop(crop: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(ImageOps.grayscale(crop), dtype=np.uint8)
    if values.size == 0:
        values = np.full((1, 1), 255, dtype=np.uint8)
    orig_h, orig_w = values.shape
    ink = (255.0 - values.astype(np.float32)) / 255.0
    metadata = np.asarray((orig_w / max(1, orig_h), orig_h / 110.0, float(ink.mean())), dtype=np.float32)
    source = Image.fromarray(np.clip(np.rint(ink * 255), 0, 255).astype(np.uint8), mode="L")
    scale = min(CONTENT_SIZE[0] / max(1, orig_w), CONTENT_SIZE[1] / max(1, orig_h))
    size = (max(1, round(orig_w * scale)), max(1, round(orig_h * scale)))
    resized = source.resize(size, Image.Resampling.BICUBIC)
    canvas = Image.new("L", CANVAS_SIZE, 0)
    canvas.paste(resized, ((28 - size[0]) // 2, (28 - size[1]) // 2))
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.35))
    return np.asarray(canvas, dtype=np.float32) / 255.0, metadata


def _directional_gradients(pixels: np.ndarray) -> np.ndarray:
    padded = np.pad(pixels, 1, mode="edge")
    horizontal = np.abs((padded[1:-1, 2:] - padded[1:-1, :-2]) * 0.5)
    vertical = np.abs((padded[2:, 1:-1] - padded[:-2, 1:-1]) * 0.5)
    plus = np.abs((padded[2:, 2:] - padded[:-2, :-2]) * 0.5)
    minus = np.abs((padded[2:, :-2] - padded[:-2, 2:]) * 0.5)
    maps = (horizontal, vertical, plus, minus)
    pooled = []
    for gradient in maps:
        for row in np.array_split(gradient, 6, axis=0):
            for cell in np.array_split(row, 6, axis=1):
                pooled.append(float(cell.mean()))
    return np.asarray(pooled, dtype=np.float32)


def features_from_canvas(canvas: np.ndarray, metadata: np.ndarray) -> np.ndarray:
    pixels = np.asarray(canvas, dtype=np.float32)
    if pixels.shape != CANVAS_SIZE: raise ValueError("expected a 28x28 canvas")
    base_image = Image.fromarray(np.clip(np.rint(pixels * 255), 0, 255).astype(np.uint8), mode="L").resize((14, 14), Image.Resampling.BICUBIC)
    base = np.asarray(base_image, dtype=np.float32).ravel() / 255.0
    horizontal = np.interp(np.linspace(0, 27, 20), np.arange(28), pixels.mean(axis=1))
    vertical = np.interp(np.linspace(0, 27, 20), np.arange(28), pixels.mean(axis=0))
    quadrants = pixels.reshape(4, 7, 4, 7).mean(axis=(1, 3)).ravel()
    result = np.concatenate((base, horizontal, vertical, quadrants, metadata, _directional_gradients(pixels), _line_features(pixels))).astype(np.float32)
    if result.shape != (FEATURE_LENGTH,): raise AssertionError(f"unexpected Type1 feature length: {result.shape}")
    return result


def extract_features(crop: Image.Image) -> np.ndarray:
    return features_from_canvas(*prepare_crop(crop))


def augment_canvas_features(crop: Image.Image, sample_index: int) -> list[np.ndarray]:
    canvas, metadata = prepare_crop(crop)
    source = Image.fromarray(np.clip(np.rint(canvas * 255), 0, 255).astype(np.uint8), mode="L")
    transforms = ((0, 0, 0), (-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0))
    output = []
    for dx, dy, angle in transforms:
        if dx or dy or angle:
            shifted = source.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, translate=(dx, dy), fillcolor=0)
            augmented = np.asarray(shifted, dtype=np.float32) / 255.0
        else:
            augmented = canvas
        output.append(features_from_canvas(augmented, metadata))
    return output


def load_labeled_crops(crops_dir: str | Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    root = Path(crops_dir)
    manifest: dict[tuple[str, str], dict[str, str]] = {}
    manifest_path = root / "manifest.csv"
    if manifest_path.is_file():
        with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                manifest[(row.get("digit", ""), row.get("crop_file", ""))] = row
    samples, skipped = [], {str(d): 0 for d in range(10)}
    for digit in map(str, range(10)):
        folder = root / digit
        for path in sorted(folder.glob("*.png")) if folder.is_dir() else []:
            with Image.open(path) as image:
                crop = image.convert("L")
                width, height = crop.size
                if height < 20 or width < 5:
                    skipped[digit] += 1
                    continue
                crop = crop.copy()
            record = manifest.get((digit, path.name), {})
            identity = record.get("blob_oid") or ":".join((record.get("source_ref", ""), record.get("source_path", "")))
            group = identity.strip(":") or path.stem.rsplit("_pos", 1)[0]
            position_text = record.get("position_1_based", "")
            position = int(position_text) if position_text else (int(path.stem.rsplit("_pos", 1)[1]) if "_pos" in path.stem else 0)
            samples.append({"path": path, "filename": path.name, "label": digit, "group": group, "position": position, "captcha_label": record.get("ground_truth", ""), "image": crop, "features": extract_features(crop)})
    return samples, skipped
