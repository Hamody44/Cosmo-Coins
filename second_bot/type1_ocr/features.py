"""Shared, noise-resistant digit feature extraction."""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .types import FEATURE_SIZE


def _components(mask: np.ndarray, min_area: int = 3) -> np.ndarray:
    """Keep foreground components large enough to be character strokes."""
    height, width = mask.shape
    seen = np.zeros((height, width), dtype=bool)
    kept = np.zeros_like(mask, dtype=bool)
    for y in range(height):
        for x in range(width):
            if not mask[y, x] or seen[y, x]:
                continue
            stack = [(x, y)]
            seen[y, x] = True
            pixels: list[tuple[int, int]] = []
            while stack:
                cx, cy = stack.pop()
                pixels.append((cx, cy))
                for nx in range(max(0, cx - 1), min(width, cx + 2)):
                    for ny in range(max(0, cy - 1), min(height, cy + 2)):
                        if not seen[ny, nx] and mask[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((nx, ny))
            if len(pixels) >= min_area:
                for px, py in pixels:
                    kept[py, px] = True
    return kept


def _normalised_foreground(crop: Image.Image) -> np.ndarray:
    gray = ImageOps.grayscale(crop)
    values = np.asarray(gray, dtype=np.uint8)
    if values.size == 0:
        return np.zeros((1, 1), dtype=bool)
    border = np.concatenate((values[0], values[-1], values[:, 0], values[:, -1]))
    centre = values[1:-1, 1:-1] if min(values.shape) > 2 else values
    if float(np.median(border)) > float(np.median(centre)):
        values = 255 - values
    background = float(np.percentile(values, 20))
    # Deliberately low threshold preserves 1px strokes; no median filter.
    threshold = max(8.0, min(96.0, background + 10.0))
    return _components(values.astype(np.float32) > threshold, min_area=3)


def extract_features(crop: Image.Image) -> np.ndarray:
    """Return one shared 24x24+aspect feature vector for train and inference."""
    foreground = _normalised_foreground(crop)
    ys, xs = np.nonzero(foreground)
    canvas = Image.new("L", FEATURE_SIZE, 0)
    aspect_ratio = 1.0
    if len(xs):
        left, right = int(xs.min()), int(xs.max()) + 1
        top, bottom = int(ys.min()), int(ys.max()) + 1
        tight = Image.fromarray((foreground[top:bottom, left:right] * 255).astype(np.uint8), mode="L")
        aspect_ratio = tight.width / max(1, tight.height)
        available = (FEATURE_SIZE[0] - 4, FEATURE_SIZE[1] - 4)
        scale = min(available[0] / tight.width, available[1] / tight.height)
        size = (max(1, round(tight.width * scale)), max(1, round(tight.height * scale)))
        tight = tight.resize(size, Image.Resampling.BICUBIC)
        tight = tight.filter(ImageFilter.GaussianBlur(radius=0.6))
        canvas.paste(tight, ((FEATURE_SIZE[0] - size[0]) // 2, (FEATURE_SIZE[1] - size[1]) // 2))
    pixels = np.asarray(canvas, dtype=np.float32) / 255.0
    return np.concatenate((pixels.ravel(), np.asarray([aspect_ratio], dtype=np.float32)))


def extract_feature(crop: Image.Image) -> list[float]:
    """Backward-compatible singular spelling used by older callers."""
    return extract_features(crop).astype(float).tolist()


def feature_distance(left: Iterable[float], right: Iterable[float]) -> float:
    a, b = list(left), list(right)
    if len(a) != len(b):
        return math.inf
    return math.dist(a, b) / math.sqrt(max(1, len(a)))
