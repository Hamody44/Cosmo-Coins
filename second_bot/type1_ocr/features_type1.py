"""Type1 OCR segmentation and compact shift-tolerant feature extraction."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps

IMAGE_SIZE = (300, 110)
CANVAS_SIZE = (20, 20)
CONTENT_SIZE = (16, 16)
FEATURE_LENGTH = 20 * 20 + 20 + 20 + 16 + 3


def box_sum_7x7(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), ((3, 3), (3, 3)), constant_values=0)
    integral = np.pad(padded, ((1, 0), (1, 0)), constant_values=0).cumsum(0).cumsum(1)
    return integral[7:, 7:] - integral[:-7, 7:] - integral[7:, :-7] + integral[:-7, :-7]


def remove_long_straight_lines(mask: np.ndarray) -> np.ndarray:
    """Remove long runs at 18 angles with nine rho offsets and density guard."""
    mask = np.asarray(mask, dtype=bool)
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if len(xs) < 80:
        return mask.copy()
    xf, yf = xs.astype(np.float32), ys.astype(np.float32)
    candidates = np.zeros(len(xs), dtype=bool)
    for angle in range(0, 180, 10):
        theta = np.deg2rad(angle)
        ct, st = float(np.cos(theta)), float(np.sin(theta))
        rho = xf * ct + yf * st
        bins = np.rint(rho).astype(np.int32)
        low = int(bins.min())
        b = bins - low
        counts = np.bincount(b)
        active = np.flatnonzero(counts >= 8)
        if not len(active):
            continue
        t = -xf * st + yf * ct
        # Count first, then inspect only the strongest accumulator bins; this
        # avoids 18 full-image scatter reductions while retaining acc >= 8.
        if len(active) > 2:
            active = active[np.argsort(counts[active])[-2:]]
        for bin_index in active:
            selected = b == bin_index
            values = t[selected]
            if float(values.max() - values.min()) < 79.0:
                continue
            center = float(np.median(rho[selected]))
            t0, t1 = float(values.min()) - 2.0, float(values.max()) + 2.0
            # The requested nine sample offsets are evaluated as one broadcast.
            offsets = np.linspace(-1.0, 1.0, 9, dtype=np.float32)[:, None]
            near = (np.abs(rho[None, :] - center - offsets) <= 0.75) & (t[None, :] >= t0) & (t[None, :] <= t1)
            candidates |= near.any(axis=0)
    density = box_sum_7x7(mask) / 49.0
    result = mask.copy()
    remove = candidates & (density[ys, xs] < 0.72)
    result[ys[remove], xs[remove]] = False
    return result


remove_long_lines = remove_long_straight_lines


def _column_projection(mask: np.ndarray) -> np.ndarray:
    h = mask.shape[0]
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    return mask[y0:y1].sum(axis=0).astype(np.float32)


def find_dividers(mask: np.ndarray) -> list[int]:
    """Globally optimize four cuts for five 24..68px slots by DP."""
    h, w = mask.shape
    projection = _column_projection(mask)
    lower, upper = int(np.ceil(0.04 * w)), int(np.floor(0.96 * w))
    cut_cost = np.zeros(w + 1, dtype=np.float32)
    cut_cost[1:w] = (projection[:-1] + projection[1:]) / max(1.0, 2.0 * h)
    inf = float("inf")
    dp = np.full((5, w + 1), inf, dtype=np.float64)
    prev = np.full((5, w + 1), -1, dtype=np.int16)
    for end in range(24, min(68, upper) + 1):
        dp[1, end] = cut_cost[end]
    for k in range(2, 5):
        for end in range(lower, upper + 1):
            lo, hi = max(lower if k == 2 else 0, end - 68), end - 24
            if lo > hi:
                continue
            starts = np.arange(lo, hi + 1)
            values = dp[k - 1, starts] + cut_cost[end]
            best = int(np.argmin(values))
            if np.isfinite(values[best]):
                dp[k, end] = values[best]
                prev[k, end] = starts[best]
    choices = [(dp[4, c], c) for c in range(lower, upper + 1) if 24 <= w - c <= 68 and np.isfinite(dp[4, c])]
    if not choices:
        return [round(w * i / 5) for i in range(1, 5)]
    _, last = min(choices, key=lambda item: (item[0], abs(item[1] - 0.8 * w)))
    cuts = [last]
    for k in range(4, 1, -1):
        cuts.append(int(prev[k, cuts[-1]]))
    return sorted(cuts)


def _components(mask: np.ndarray) -> list[tuple[int, int, int, int, int, float, float]]:
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    result = []
    for y, x in np.argwhere(mask):
        y, x = int(y), int(x)
        if seen[y, x]: continue
        seen[y, x] = True; stack = [(y, x)]
        area = sx = sy = 0; left = right = x; top = bottom = y
        while stack:
            cy, cx = stack.pop(); area += 1; sx += cx; sy += cy
            left = min(left, cx); right = max(right, cx); top = min(top, cy); bottom = max(bottom, cy)
            for ny, nx in ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny,nx] and not seen[ny,nx]:
                    seen[ny,nx] = True; stack.append((ny,nx))
        result.append((left, top, right + 1, bottom + 1, area, sx / area, sy / area))
    return result


def tight_digit_crop(mask: np.ndarray, x0: int, x1: int) -> np.ndarray:
    h, _ = mask.shape; slot = mask[:, x0:x1]; kept = np.zeros_like(slot); sw = max(1, x1 - x0)
    for left, top, right, bottom, area, cx, cy in _components(slot):
        if area >= 40 and 0.15 <= cx / sw <= 0.85 and 0.22 * h <= cy <= 0.78 * h:
            kept[top:bottom, left:right] |= slot[top:bottom, left:right]
    if not kept.any(): kept = slot
    ys, xs = np.nonzero(kept)
    if not len(xs): return np.full((1, 1), 255, dtype=np.uint8)
    left, right = max(0, int(xs.min()) - 2), min(sw, int(xs.max()) + 3)
    top, bottom = max(0, int(ys.min()) - 2), min(h, int(ys.max()) + 3)
    return np.where(kept[top:bottom, left:right], 0, 255).astype(np.uint8)


def segment_type1_image(image: Image.Image) -> list[Image.Image]:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.size != IMAGE_SIZE: raise ValueError(f"Type1 segmentation requires {IMAGE_SIZE[0]}x{IMAGE_SIZE[1]}, got {image.size}")
    cleaned = remove_long_straight_lines(np.mean(np.asarray(image, dtype=np.uint8), axis=2) < 115)
    cuts = [0, *find_dividers(cleaned), cleaned.shape[1]]
    return [Image.fromarray(tight_digit_crop(cleaned, cuts[i], cuts[i+1]), mode="L") for i in range(5)]


def prepare_crop(crop: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(ImageOps.grayscale(crop), dtype=np.uint8)
    if values.size == 0: values = np.full((1, 1), 255, dtype=np.uint8)
    orig_h, orig_w = values.shape; ink = (255.0 - values.astype(np.float32)) / 255.0
    metadata = np.asarray((orig_w / max(1, orig_h), orig_h / 110.0, float(ink.mean())), dtype=np.float32)
    source = Image.fromarray(np.clip(np.rint(ink * 255), 0, 255).astype(np.uint8), mode="L")
    scale = min(16 / max(1, orig_w), 16 / max(1, orig_h)); size = (max(1, round(orig_w * scale)), max(1, round(orig_h * scale)))
    resized = source.resize(size, Image.Resampling.BICUBIC); canvas = Image.new("L", (20, 20), 0)
    canvas.paste(resized, ((20-size[0])//2, (20-size[1])//2)); canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.7))
    return np.asarray(canvas, dtype=np.float32) / 255.0, metadata


def features_from_canvas(canvas: np.ndarray, metadata: np.ndarray) -> np.ndarray:
    pixels = np.asarray(canvas, dtype=np.float32)
    if pixels.shape != (20, 20): raise ValueError("expected a 20x20 canvas")
    result = np.concatenate((pixels.ravel(), pixels.mean(axis=1), pixels.mean(axis=0), pixels.reshape(4,5,4,5).mean(axis=(1,3)).ravel(), metadata)).astype(np.float32)
    if result.shape != (FEATURE_LENGTH,): raise AssertionError(f"unexpected Type1 feature length: {result.shape}")
    return result


def extract_features(crop: Image.Image) -> np.ndarray: return features_from_canvas(*prepare_crop(crop))


def augment_canvas_features(crop: Image.Image, sample_index: int) -> list[np.ndarray]:
    canvas, metadata = prepare_crop(crop); source = Image.fromarray(np.clip(np.rint(canvas * 255), 0, 255).astype(np.uint8), mode="L")
    transforms = ((0,0,0),(-1,0,-3),(1,0,3),(0,-1 if sample_index % 2 else 1,0)); output=[]
    for dx,dy,angle in transforms:
        if dx or dy or angle: image = source.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, translate=(dx,dy), fillcolor=0); value=np.asarray(image,dtype=np.float32)/255.0
        else: value=canvas
        output.append(features_from_canvas(value, metadata))
    return output


def load_labeled_crops(crops_dir: str | Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    root=Path(crops_dir); manifest={}; path=root/'manifest.csv'
    if path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f): manifest[(row.get('digit',''),row.get('crop_file',''))]=row
    samples=[]; skipped={str(d):0 for d in range(10)}
    for digit in map(str,range(10)):
        for p in sorted((root/digit).glob('*.png')) if (root/digit).is_dir() else []:
            with Image.open(p) as im: crop=im.convert('L'); width,height=crop.size; crop=crop.copy()
            if height<20 or width<5: skipped[digit]+=1; continue
            row=manifest.get((digit,p.name),{}); identity=row.get('blob_oid') or ':'.join((row.get('source_ref',''),row.get('source_path',''))); group=identity.strip(':') or p.stem.rsplit('_pos',1)[0]
            pos=row.get('position_1_based',''); position=int(pos) if pos else (int(p.stem.rsplit('_pos',1)[1]) if '_pos' in p.stem else 0)
            samples.append({'path':p,'filename':p.name,'label':digit,'group':group,'position':position,'captcha_label':row.get('ground_truth',''),'image':crop,'features':extract_features(crop)})
    return samples, skipped
