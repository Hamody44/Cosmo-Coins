"""Shared Type1 CAPTCHA segmentation and scikit-learn feature extraction.

The segmentation functions below are the same deterministic Pillow/NumPy
pipeline used to create the checked-in ``digits_crops`` training images.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

IMAGE_SIZE = (300, 110)
CANVAS_SIZE = (32, 32)
CONTENT_SIZE = (28, 28)  # 2px minimum margin on each side.
FEATURE_LENGTH = 32 * 32 + 8 * 8 + 32 + 32 + 3
DIVIDER_RATIOS = (0.25, 0.41, 0.57, 0.74)


def box_sum_7x7(mask: np.ndarray) -> np.ndarray:
    """Return the clipped-border 7x7 ink-pixel counts at every image pixel."""
    padded = np.pad(mask.astype(np.uint8), ((3, 3), (3, 3)), constant_values=0)
    integral = np.pad(padded, ((1, 0), (1, 0)), constant_values=0).cumsum(0).cumsum(1)
    return (
        integral[7:, 7:] - integral[:-7, 7:]
        - integral[7:, :-7] + integral[:-7, :-7]
    )


def remove_long_lines(mask: np.ndarray) -> np.ndarray:
    """Erase >=80px line runs at 5-degree steps only below 0.72 local density."""
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if len(xs) < 80:
        return mask.copy()

    xf, yf = xs.astype(np.float64), ys.astype(np.float64)
    candidates = np.zeros(len(xs), dtype=bool)
    for angle in range(0, 176, 5):
        theta = math.radians(angle)
        ct, st = math.cos(theta), math.sin(theta)
        rho_float = xf * ct + yf * st
        rho_bins = np.rint(rho_float).astype(np.int32)
        t = -xf * st + yf * ct
        low = int(rho_bins.min())
        counts = np.bincount(rho_bins - low)
        for bin_index in np.flatnonzero(counts >= 80):
            indices = np.flatnonzero(rho_bins == bin_index + low)
            if len(indices) < 80:
                continue
            order = np.argsort(t[indices])
            sorted_indices = indices[order]
            sorted_t = t[sorted_indices]
            breaks = np.flatnonzero(np.diff(sorted_t) > 3.0) + 1
            for group in np.split(sorted_indices, breaks):
                if len(group) < 80 or t[group].max() - t[group].min() < 79.0:
                    continue
                rho_center = float(np.median(rho_float[group]))
                t0, t1 = float(t[group].min()) - 2.0, float(t[group].max()) + 2.0
                near = (np.abs(rho_float - rho_center) <= 1.25) & (t >= t0) & (t <= t1)
                candidates |= near

    density = box_sum_7x7(mask) / 49.0
    result = mask.copy()
    remove = candidates & (density[ys, xs] < 0.72)
    result[ys[remove], xs[remove]] = False
    return result


def find_dividers(mask: np.ndarray) -> list[int]:
    """Find four low-ink cuts in the central band near the five-slot priors."""
    h, w = mask.shape
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    projection = mask[y0:y1].sum(axis=0)
    radius = int(round(0.06 * w))
    dividers = []
    for ratio in DIVIDER_RATIOS:
        expected = ratio * w
        lo = max(1, int(math.floor(expected - radius)))
        hi = min(w - 1, int(math.ceil(expected + radius)) + 1)
        positions = np.arange(lo, hi)
        values = projection[lo:hi]
        tied = positions[values == values.min()]
        dividers.append(int(min(tied, key=lambda x: abs(x - expected))))
    return dividers


def tight_digit_crop(mask: np.ndarray, x0: int, x1: int) -> np.ndarray:
    """Return a white-background crop based on the strongest connected glyph."""
    h, w = mask.shape
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    region = mask[:, x0:x1]
    col_count = region[y0:y1].sum(axis=0)
    global_x = np.arange(x0, x1)
    support_floor = max(2, int(round(0.06 * (y1 - y0))))
    valid = (col_count >= support_floor) & (global_x >= int(0.04 * w)) & (global_x < int(0.96 * w))
    supported = np.flatnonzero(valid)
    if len(supported) == 0:
        supported = np.flatnonzero(col_count > 0)
    if len(supported) == 0:
        supported = np.flatnonzero(region.any(axis=0))
    if len(supported) == 0:
        return np.full((1, 1), 255, dtype=np.uint8)

    breaks = np.flatnonzero(np.diff(supported) > 3) + 1
    runs = np.split(supported, breaks)
    active = max(runs, key=lambda run: int(col_count[run].sum()))
    left, right = x0 + int(active[0]), x0 + int(active[-1]) + 1
    core = mask[:, left:right]
    seen = np.zeros(core.shape, dtype=bool)
    best_area, best_box = 0, None
    for start_y, start_x in np.argwhere(core):
        if seen[start_y, start_x]:
            continue
        seen[start_y, start_x] = True
        stack = [(int(start_y), int(start_x))]
        min_y = max_y = int(start_y)
        min_x = max_x = int(start_x)
        area = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            min_y, max_y = min(min_y, cy), max(max_y, cy)
            min_x, max_x = min(min_x, cx), max(max_x, cx)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if (dy or dx) and 0 <= ny < core.shape[0] and 0 <= nx < core.shape[1]:
                        if core[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
        if area > best_area:
            best_area = area
            best_box = (min_y, max_y + 1, min_x, max_x + 1)

    if best_box is None:
        return np.full((1, 1), 255, dtype=np.uint8)
    top, bottom, rel_left, rel_right = best_box
    left = max(x0, left + rel_left - 2)
    right = min(x1, left + (rel_right - rel_left) + 4)
    top, bottom = max(0, top - 2), min(h, bottom + 2)
    digit = mask[top:bottom, left:right]
    return np.where(digit, 0, 255).astype(np.uint8)


def segment_type1_image(image: Image.Image) -> list[Image.Image]:
    """Apply the exact RGB-threshold, line-removal, projection, and crop path."""
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.size != IMAGE_SIZE:
        raise ValueError(f"Type1 segmentation requires {IMAGE_SIZE[0]}x{IMAGE_SIZE[1]}, got {image.size}")
    rgb = np.asarray(image, dtype=np.uint8)
    ink = np.mean(rgb, axis=2) < 115
    cleaned = remove_long_lines(ink)
    cuts = [0, *find_dividers(cleaned), cleaned.shape[1]]
    return [
        Image.fromarray(tight_digit_crop(cleaned, cuts[position], cuts[position + 1]), mode="L")
        for position in range(5)
    ]


def prepare_crop(crop: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """Normalize a crop to 32x32 ink intensities and return three raw metadata features."""
    gray = ImageOps.grayscale(crop)
    values = np.asarray(gray, dtype=np.uint8)
    if values.size == 0:
        values = np.full((1, 1), 255, dtype=np.uint8)
    orig_h, orig_w = values.shape
    ink = (255.0 - values.astype(np.float32)) / 255.0
    aspect = float(orig_w / max(1, orig_h))
    density = float(ink.mean())

    ink_image = Image.fromarray(np.clip(np.rint(ink * 255.0), 0, 255).astype(np.uint8), mode="L")
    scale = min(CONTENT_SIZE[0] / max(1, orig_w), CONTENT_SIZE[1] / max(1, orig_h))
    new_size = (
        max(1, min(CONTENT_SIZE[0], int(round(orig_w * scale)))),
        max(1, min(CONTENT_SIZE[1], int(round(orig_h * scale)))),
    )
    resized = ink_image.resize(new_size, Image.Resampling.BICUBIC)
    canvas_image = Image.new("L", CANVAS_SIZE, 0)
    offset = ((CANVAS_SIZE[0] - new_size[0]) // 2, (CANVAS_SIZE[1] - new_size[1]) // 2)
    canvas_image.paste(resized, offset)
    canvas = np.asarray(canvas_image, dtype=np.float32) / 255.0
    metadata = np.asarray((aspect, orig_h / 110.0, density), dtype=np.float32)
    return canvas, metadata


def features_from_canvas(canvas: np.ndarray, metadata: np.ndarray) -> np.ndarray:
    """Build the unified 1155-value feature vector from a prepared ink canvas."""
    pixels = np.asarray(canvas, dtype=np.float32)
    if pixels.shape != CANVAS_SIZE:
        raise ValueError(f"expected a {CANVAS_SIZE[0]}x{CANVAS_SIZE[1]} canvas")
    grid = pixels.reshape(8, 4, 8, 4).mean(axis=(1, 3)).ravel()
    horizontal = pixels.mean(axis=1)
    vertical = pixels.mean(axis=0)
    result = np.concatenate((pixels.ravel(), grid, horizontal, vertical, np.asarray(metadata, dtype=np.float32)))
    if result.shape != (FEATURE_LENGTH,):
        raise AssertionError(f"unexpected Type1 feature length: {result.shape}")
    return result.astype(np.float32, copy=False)


def extract_features(crop: Image.Image) -> np.ndarray:
    """Return flattened pixels, 8x8 block averages, projections, and metadata."""
    canvas, metadata = prepare_crop(crop)
    return features_from_canvas(canvas, metadata)


def augment_canvas_features(crop: Image.Image, sample_index: int) -> list[np.ndarray]:
    """Return original plus three light shifts/rotations (~4x total samples)."""
    canvas, metadata = prepare_crop(crop)
    source = Image.fromarray(np.clip(np.rint(canvas * 255.0), 0, 255).astype(np.uint8), mode="L")
    vertical_shift = 1 if sample_index % 2 == 0 else -1
    vertical_angle = 3 if sample_index % 2 == 0 else -3
    transforms = ((0, 0, 0), (-1, 0, -3), (1, 0, 3), (0, vertical_shift, vertical_angle))
    output = []
    for dx, dy, angle in transforms:
        if angle == 0 and dx == 0 and dy == 0:
            augmented = canvas
        else:
            shifted = source.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=False,
                translate=(dx, dy),
                fillcolor=0,
            )
            augmented = np.asarray(shifted, dtype=np.float32) / 255.0
        output.append(features_from_canvas(augmented, metadata))
    return output


def load_labeled_crops(crops_dir: str | Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Load digit PNGs, skip original crops <20px tall or <5px wide, and group by source captcha."""
    root = Path(crops_dir)
    manifest_path = root / "manifest.csv"
    manifest: dict[tuple[str, str], dict[str, str]] = {}
    if manifest_path.is_file():
        with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                manifest[(row.get("digit", ""), row.get("crop_file", ""))] = row

    samples: list[dict[str, Any]] = []
    skipped = {str(digit): 0 for digit in range(10)}
    for digit in map(str, range(10)):
        folder = root / digit
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.png")):
            with Image.open(path) as image:
                crop = image.convert("L")
                width, height = crop.size
                if height < 20 or width < 5:
                    skipped[digit] += 1
                    continue
                crop = crop.copy()
            record = manifest.get((digit, path.name), {})
            if record and record.get("digit") != digit:
                raise ValueError(f"label mismatch for {path}")
            source_identity = record.get("blob_oid")
            if not source_identity:
                source_identity = ":".join((record.get("source_ref", ""), record.get("source_path", "")))
            group = source_identity.strip(":") or path.stem.rsplit("_pos", 1)[0]
            pos_text = record.get("position_1_based", "")
            if pos_text:
                position = int(pos_text)
            else:
                match = path.stem.rsplit("_pos", 1)
                position = int(match[1]) if len(match) == 2 and match[1].isdigit() else 0
            samples.append({
                "path": path,
                "filename": path.name,
                "label": digit,
                "group": group,
                "position": position,
                "captcha_label": record.get("ground_truth", ""),
                "image": crop,
                "features": extract_features(crop),
            })
    return samples, skipped
