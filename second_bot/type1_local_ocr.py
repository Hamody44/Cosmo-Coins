"""Lightweight local OCR specialized for five-digit type1 images.

This module is intentionally independent from local_ocr.py so type2 behavior
cannot change accidentally. It uses Pillow only, several image masks, compact
pixel/HOG features, position-aware KNN templates, and confidence thresholds.
"""
from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

IMAGE_SIZE = (300, 110)
CELL_COUNT = 5
FEATURE_SIZE = (32, 48)
PIXEL_FEATURE_SIZE = (16, 24)
CELL_SIZE = 8
HOG_BINS = 9
PREPROCESSING_VARIANTS = ("contrast", "otsu", "adaptive", "raw")
DEFAULT_MIN_SIMILARITY = 0.62
DEFAULT_MIN_MARGIN = 0.035
DEFAULT_MIN_AVERAGE_CONFIDENCE = 0.70


def load_image(source: bytes | str | Path | Image.Image) -> Image.Image:
    if isinstance(source, Image.Image):
        image = source.copy()
    elif isinstance(source, bytes):
        image = Image.open(BytesIO(source))
    else:
        image = Image.open(source)
    image.load()
    return ImageOps.exif_transpose(image).convert("RGB").resize(
        IMAGE_SIZE, Image.Resampling.LANCZOS
    )


def otsu_threshold(image: Image.Image) -> int:
    histogram = image.convert("L").histogram()
    total = sum(histogram)
    if not total:
        return 127
    weighted_total = sum(level * count for level, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    best_threshold = 127
    for threshold, count in enumerate(histogram):
        background_weight += count
        background_sum += threshold * count
        foreground_weight = total - background_weight
        if not background_weight or not foreground_weight:
            continue
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _dark_mask(gray: Image.Image, threshold: int) -> Image.Image:
    return gray.point(lambda value: 255 if value < threshold else 0, mode="L")


def _remove_small_noise(mask: Image.Image) -> Image.Image:
    # A small median pass is much cheaper than OpenCV morphology and preserves
    # the connected strokes used by this CAPTCHA style.
    return mask.filter(ImageFilter.MedianFilter(size=3))


def make_masks(image: Image.Image) -> dict[str, Image.Image]:
    """Create complementary foreground masks; white pixels represent digits."""
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    enhanced = ImageEnhance.Contrast(gray).enhance(1.8)
    enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.1, percent=150, threshold=3))

    threshold = otsu_threshold(enhanced)
    otsu = _remove_small_noise(_dark_mask(enhanced, threshold))

    adaptive_gray = gray.filter(ImageFilter.MedianFilter(size=3))
    background = adaptive_gray.filter(ImageFilter.GaussianBlur(radius=7))
    darkness = ImageChops.subtract(background, adaptive_gray)
    adaptive = darkness.point(lambda value: 255 if value >= 18 else 0, mode="L")
    adaptive = _remove_small_noise(adaptive)

    raw = _remove_small_noise(_dark_mask(gray, 110))
    contrast = _remove_small_noise(_dark_mask(enhanced, 115))
    return {"contrast": contrast, "otsu": otsu, "adaptive": adaptive, "raw": raw}


def _crop_cell(mask: Image.Image, position: int) -> Image.Image:
    width, height = mask.size
    cell_width = width / CELL_COUNT
    # A small overlap helps when a glyph touches a fixed cell boundary.
    left = max(0, round(position * cell_width) - 2)
    right = min(width, round((position + 1) * cell_width) + 2)
    return mask.crop((left, 0, right, height))


def _trim_foreground(cell: Image.Image) -> Image.Image:
    # Ignore isolated border noise when finding the glyph box.
    projection = []
    pixels = cell.load()
    for x in range(cell.width):
        projection.append(sum(1 for y in range(cell.height) if pixels[x, y] > 0))
    active = [index for index, count in enumerate(projection) if count >= 2]
    if not active:
        return cell
    left, right = max(0, min(active) - 2), min(cell.width, max(active) + 3)
    return cell.crop((left, 0, right, cell.height))


def normalize_mask(mask: Image.Image) -> Image.Image:
    mask = _trim_foreground(mask.convert("L"))
    box = mask.getbbox()
    canvas = Image.new("L", FEATURE_SIZE, 0)
    if not box:
        return canvas
    glyph = mask.crop(box)
    scale = min((FEATURE_SIZE[0] - 6) / max(1, glyph.width), (FEATURE_SIZE[1] - 6) / max(1, glyph.height))
    size = (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale)))
    glyph = glyph.resize(size, Image.Resampling.BILINEAR)
    canvas.paste(glyph, ((FEATURE_SIZE[0] - size[0]) // 2, (FEATURE_SIZE[1] - size[1]) // 2))
    return canvas


def hog_feature(mask: Image.Image) -> list[float]:
    width, height = FEATURE_SIZE
    pixels = [value / 255.0 for value in mask.tobytes()]
    columns, rows = width // CELL_SIZE, height // CELL_SIZE
    histograms = [[0.0] * HOG_BINS for _ in range(columns * rows)]
    for y in range(height):
        for x in range(width):
            x0, x1 = max(0, x - 1), min(width - 1, x + 1)
            y0, y1 = max(0, y - 1), min(height - 1, y + 1)
            gx = pixels[y * width + x1] - pixels[y * width + x0]
            gy = pixels[y1 * width + x] - pixels[y0 * width + x]
            magnitude = math.hypot(gx, gy)
            if magnitude == 0:
                continue
            angle = math.atan2(gy, gx) % math.pi
            bin_index = min(HOG_BINS - 1, int(angle * HOG_BINS / math.pi))
            histograms[(y // CELL_SIZE) * columns + (x // CELL_SIZE)][bin_index] += magnitude
    result: list[float] = []
    for histogram in histograms:
        norm = math.sqrt(sum(value * value for value in histogram)) + 1e-6
        result.extend(value / norm for value in histogram)
    norm = math.sqrt(sum(value * value for value in result)) + 1e-6
    return [value / norm for value in result]


def digit_feature(mask: Image.Image) -> list[float]:
    normalized = normalize_mask(mask)
    small = normalized.resize(PIXEL_FEATURE_SIZE, Image.Resampling.BILINEAR)
    pixels = [value / 255.0 for value in small.tobytes()]
    dark_ratio = sum(pixels) / max(1, len(pixels))
    box = normalized.getbbox()
    if box:
        x0, y0, x1, y1 = box
        geometry = [x0 / 32, y0 / 48, x1 / 32, y1 / 48, (x1 - x0) / 32, (y1 - y0) / 48]
    else:
        geometry = [0.0] * 6
    return hog_feature(normalized) + pixels + [dark_ratio] + geometry


def extract_features(source: bytes | str | Path | Image.Image) -> dict[str, list[list[float]]]:
    image = load_image(source)
    masks = make_masks(image)
    return {
        variant: [digit_feature(_crop_cell(mask, position)) for position in range(CELL_COUNT)]
        for variant, mask in masks.items()
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    return sum(a * b for a, b in zip(left, right))


def _rank_digit_candidates(
    feature: list[float], templates: list[dict[str, Any]], *, k: int = 5
) -> list[tuple[float, str]]:
    ranked = sorted(
        ((cosine_similarity(feature, item["feature"]), str(item["digit"])) for item in templates),
        reverse=True,
    )
    return ranked[: max(1, k)]


def predict_features(
    features: dict[str, list[list[float]]],
    templates: list[dict[str, Any]],
    *,
    exclude_filename: str | None = None,
    k: int = 5,
) -> dict[str, Any]:
    digits: list[str] = []
    confidence: list[dict[str, float | str]] = []
    for position in range(CELL_COUNT):
        candidates = [
            template for template in templates
            if template.get("position") == position
            and (exclude_filename is None or template.get("filename") != exclude_filename)
        ]
        ranked_all: list[tuple[float, str]] = []
        for variant in PREPROCESSING_VARIANTS:
            variant_features = features.get(variant)
            if variant_features:
                variant_candidates = [
                    template for template in candidates
                    if template.get("variant") == variant
                ]
                ranked_all.extend(
                    _rank_digit_candidates(variant_features[position], variant_candidates, k=k)
                )
        if not ranked_all:
            return {"text": "", "reliable": False, "confidence": []}
        votes: dict[str, float] = {}
        best_by_digit: dict[str, float] = {}
        for similarity, digit in ranked_all:
            weight = max(0.0, similarity)
            votes[digit] = votes.get(digit, 0.0) + weight
            best_by_digit[digit] = max(similarity, best_by_digit.get(digit, -1.0))
        ordered = sorted(votes.items(), key=lambda item: item[1], reverse=True)
        digit, top_vote = ordered[0]
        second_vote = ordered[1][1] if len(ordered) > 1 else 0.0
        total_vote = sum(votes.values()) or 1.0
        digits.append(digit)
        confidence.append({
            "digit": digit,
            "similarity": float(best_by_digit[digit]),
            "vote_confidence": float(top_vote / total_vote),
            "margin": float((top_vote - second_vote) / total_vote),
        })
    average = sum(float(item["vote_confidence"]) for item in confidence) / CELL_COUNT
    minimum = min(float(item["vote_confidence"]) for item in confidence)
    minimum_margin = min(float(item["margin"]) for item in confidence)
    reliable = (
        len("".join(digits)) == CELL_COUNT
        and minimum >= DEFAULT_MIN_SIMILARITY
        and average >= DEFAULT_MIN_AVERAGE_CONFIDENCE
        and minimum_margin >= DEFAULT_MIN_MARGIN
    )
    return {
        "text": "".join(digits),
        "reliable": reliable,
        "average_confidence": average,
        "min_confidence": minimum,
        "min_margin": minimum_margin,
        "confidence": confidence,
    }


def recognize(
    source: bytes | str | Path | Image.Image,
    model: dict[str, Any],
    *,
    exclude_filename: str | None = None,
) -> dict[str, Any]:
    features = extract_features(source)
    templates = model.get("templates", [])
    result = predict_features(features, templates, exclude_filename=exclude_filename)
    result["type"] = "type1"
    result["model_name"] = model.get("model_name", "type1-local")
    return result
