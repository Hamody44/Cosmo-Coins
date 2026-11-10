"""Lightweight, experimental, character-wise CAPTCHA OCR using Pillow only.

The model is trained from captcha_samples/label.csv by train_local_ocr.py.
It classifies digits independently, so it can form sequences not seen during
training. The full-number baseline is evaluated by the trainer but is not used
for inference because it can only retrieve stored answers.
"""
from __future__ import annotations

import json
import math
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

IMAGE_SIZE = (300, 110)
FEATURE_SIZE = (32, 48)
CELL_SIZE = 8
HOG_BINS = 9
SETTINGS = {
    "type1": {"threshold": 100, "opening": 5},
    "type2": {"threshold": 150, "opening": 3},
}


def load_image(source: bytes | str | Path | Image.Image) -> Image.Image:
    if isinstance(source, Image.Image):
        image = source.copy()
    elif isinstance(source, bytes):
        image = Image.open(BytesIO(source))
    else:
        image = Image.open(source)
    image.load()
    if image.size != IMAGE_SIZE:
        image = image.resize(IMAGE_SIZE, Image.Resampling.LANCZOS)
    return image.convert("RGB")


def detect_style(image: Image.Image) -> str:
    """Distinguish the light, noisy style from the dark, colored style."""
    gray = ImageOps.grayscale(load_image(image))
    px = gray.load()
    samples = []
    # Use corner patches so the glyph colors and crossing lines matter less.
    for x0, y0 in ((0, 0), (270, 0), (0, 80), (270, 80)):
        for y in range(y0, y0 + 20):
            for x in range(x0, x0 + 30):
                samples.append(px[x, y])
    samples.sort()
    median = samples[len(samples) // 2]
    return "type2" if median < 125 else "type1"


def _digit_crop(image: Image.Image, style: str, position: int) -> Image.Image:
    if style == "type1":
        left, right = position * 60, (position + 1) * 60
    elif style == "type2":
        left = 27 + position * 45
        right = left + 34
    else:
        raise ValueError(f"Unknown CAPTCHA style: {style}")
    return image.crop((left, 0, right, IMAGE_SIZE[1]))


def _mask_digit(image: Image.Image, style: str) -> Image.Image:
    gray = ImageOps.grayscale(image)
    config = SETTINGS[style]
    threshold = config["threshold"]
    if style == "type1":
        mask = gray.point(lambda value: 255 if value < threshold else 0)
    else:
        mask = gray.point(lambda value: 255 if value > threshold else 0)
    opening = config["opening"]
    if opening:
        mask = mask.filter(ImageFilter.MinFilter(opening)).filter(ImageFilter.MaxFilter(opening))
    return mask


def _normalize_mask(mask: Image.Image) -> Image.Image:
    box = mask.getbbox()
    width, height = FEATURE_SIZE
    canvas = Image.new("L", FEATURE_SIZE, 0)
    if not box:
        return canvas
    mask = mask.crop(box)
    scale = min((width - 4) / mask.width, (height - 4) / mask.height)
    new_width = max(1, round(mask.width * scale))
    new_height = max(1, round(mask.height * scale))
    mask = mask.resize((new_width, new_height), Image.Resampling.BILINEAR)
    canvas.paste(mask, ((width - new_width) // 2, (height - new_height) // 2))
    return canvas


def hog_feature(mask: Image.Image) -> list[float]:
    """Compute a small 4x6-cell, 9-bin unsigned-gradient HOG descriptor."""
    width, height = FEATURE_SIZE
    pixels = [value / 255.0 for value in mask.tobytes()]
    cell_columns = width // CELL_SIZE
    cell_rows = height // CELL_SIZE
    histograms = [[0.0] * HOG_BINS for _ in range(cell_columns * cell_rows)]

    for y in range(height):
        y0, y1 = max(0, y - 1), min(height - 1, y + 1)
        for x in range(width):
            x0, x1 = max(0, x - 1), min(width - 1, x + 1)
            gx = pixels[y * width + x1] - pixels[y * width + x0]
            gy = pixels[y1 * width + x] - pixels[y0 * width + x]
            magnitude = math.hypot(gx, gy)
            if magnitude == 0:
                continue
            angle = math.atan2(gy, gx) % math.pi
            bin_index = min(HOG_BINS - 1, int(angle * HOG_BINS / math.pi))
            cell_x, cell_y = x // CELL_SIZE, y // CELL_SIZE
            histograms[cell_y * cell_columns + cell_x][bin_index] += magnitude

    feature: list[float] = []
    for histogram in histograms:
        norm = math.sqrt(sum(value * value for value in histogram)) + 1e-6
        feature.extend(value / norm for value in histogram)
    norm = math.sqrt(sum(value * value for value in feature)) + 1e-6
    return [value / norm for value in feature]


def digit_features(image: Image.Image, style: str) -> list[list[float]]:
    image = load_image(image)
    if style not in SETTINGS:
        raise ValueError(f"Unknown CAPTCHA style: {style}")
    return [
        hog_feature(_normalize_mask(_mask_digit(_digit_crop(image, style, position), style)))
        for position in range(5)
    ]


def full_image_feature(image: Image.Image, style: str) -> list[int]:
    """Compact feature for the full-sequence nearest-neighbor baseline."""
    gray = ImageOps.grayscale(load_image(image))
    if style == "type1":
        mask = gray.point(lambda value: 1 if value < 100 else 0)
    else:
        mask = gray.point(lambda value: 1 if value > 150 else 0)
    mask = mask.resize((60, 22), Image.Resampling.NEAREST)
    return [int(value > 0) for value in mask.tobytes()]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def predict_digit_features(
    features: list[list[float]],
    templates: list[dict[str, Any]],
    *,
    exclude_filename: str | None = None,
) -> dict[str, Any]:
    output = ""
    confidences = []
    for feature in features:
        best_by_digit: dict[str, float] = {}
        for template in templates:
            if exclude_filename and template.get("filename") == exclude_filename:
                continue
            similarity = cosine_similarity(feature, template["feature"])
            digit = str(template["digit"])
            best_by_digit[digit] = max(similarity, best_by_digit.get(digit, -1.0))
        ranked = sorted(best_by_digit.items(), key=lambda item: item[1], reverse=True)
        if not ranked:
            return {"text": "", "confidence": [], "scores": []}
        output += ranked[0][0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        confidences.append({"digit": ranked[0][0], "similarity": ranked[0][1], "margin": ranked[0][1] - second})
    return {"text": output, "confidence": confidences}


def load_model(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        model = json.load(handle)
    if model.get("schema_version") != 1 or not model.get("templates"):
        raise ValueError("Unsupported or empty local OCR model")
    return model


def recognize(source: bytes | str | Path | Image.Image, model: dict[str, Any]) -> dict[str, Any]:
    image = load_image(source)
    style = detect_style(image)
    features = digit_features(image, style)
    templates = [template for template in model["templates"] if template["type"] == style]
    result = predict_digit_features(features, templates)
    result["type"] = style
    return result
