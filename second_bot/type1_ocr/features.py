"""Position-independent glyph features and deterministic distance metrics."""
from __future__ import annotations

import math

from PIL import Image

from .types import FEATURE_SIZE


def extract_feature(segment: Image.Image) -> list[float]:
    """Normalize one binary character crop and append row/column projections."""
    box = segment.getbbox()
    canvas = Image.new("L", FEATURE_SIZE, 0)
    if box:
        crop = segment.crop(box)
        scale = min((FEATURE_SIZE[0] - 4) / crop.width, (FEATURE_SIZE[1] - 4) / crop.height)
        size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        crop = crop.resize(size, Image.Resampling.BILINEAR)
        canvas.paste(crop, ((FEATURE_SIZE[0] - size[0]) // 2, (FEATURE_SIZE[1] - size[1]) // 2))
    values = [pixel / 255.0 for pixel in canvas.tobytes()]
    pixels = canvas.load()
    for y in range(FEATURE_SIZE[1]):
        values.append(sum(pixels[x, y] for x in range(FEATURE_SIZE[0])) / (255 * FEATURE_SIZE[0]))
    for x in range(FEATURE_SIZE[0]):
        values.append(sum(pixels[x, y] for y in range(FEATURE_SIZE[1])) / (255 * FEATURE_SIZE[1]))
    return values


def feature_distance(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return math.inf
    # math.dist performs the same Euclidean accumulation in optimized C,
    # avoiding a Python generator for every 1,024-value template comparison.
    return math.dist(left, right) / math.sqrt(max(1, len(left)))
