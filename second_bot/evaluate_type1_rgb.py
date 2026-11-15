#!/usr/bin/env python3
"""Evaluate RGB/color preprocessing variants for the Pillow-only type1 OCR.

Each variant is evaluated independently on the same deterministic holdout. The
variant's templates are built from training rows only; validation rows are never
used to choose thresholds, templates, or the reported winner.

Example:
    PYTHONPATH=second_bot python3 second_bot/evaluate_type1_rgb.py \
        --data samples --report type1_rgb_report.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

from train_type1_local_ocr import edit_distance, read_dataset, split_rows
from type1_local_ocr import CELL_COUNT, _feature, _gray, load_image, segment_candidates


def channel_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """Return color projections; all output images are grayscale."""
    rgb = load_image(image)
    r, g, b = rgb.split()
    darker = ImageChops.darker(ImageChops.darker(r, g), b)
    lighter = ImageChops.lighter(ImageChops.lighter(r, g), b)
    spread = Image.eval(Image.new("L", rgb.size, 0), lambda _: 0)
    sp = spread.load()
    rp, gp, bp = r.load(), g.load(), b.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            sp[x, y] = max(rp[x, y], gp[x, y], bp[x, y]) - min(rp[x, y], gp[x, y], bp[x, y])
    return [
        ("gray", _gray(rgb)),
        ("red", r), ("green", g), ("blue", b),
        ("min_channel", darker), ("max_channel", lighter),
        ("red_minus_green", ImageChops.subtract(r, g, scale=1, offset=128)),
        ("red_minus_blue", ImageChops.subtract(r, b, scale=1, offset=128)),
        ("green_minus_blue", ImageChops.subtract(g, b, scale=1, offset=128)),
        ("color_spread", spread),
    ]


def mask_variants(gray: Image.Image, *, thresholds: tuple[int, ...]) -> list[tuple[str, Image.Image]]:
    """Make both polarities plus a background-relative mask."""
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=7))
    result: list[tuple[str, Image.Image]] = []
    for threshold in thresholds:
        result.append((f"dark_{threshold}", gray.point(lambda p, t=threshold: 255 if p < t else 0)))
        result.append((f"light_{threshold}", gray.point(lambda p, t=threshold: 255 if p > t else 0)))
    local = Image.new("L", gray.size, 0)
    gp, bp, lp = gray.load(), blurred.load(), local.load()
    for y in range(gray.height):
        for x in range(gray.width):
            delta = gp[x, y] - bp[x, y]
            lp[x, y] = 255 if abs(delta) >= 18 and delta < 0 else 0
    result.append(("background_dark", local))
    return result


def feature_for_image(image: Image.Image, *, max_candidates: int) -> list[list[float]]:
    """Choose a bounded geometry-based split, without using the answer label."""
    best: tuple[float, list[Image.Image]] | None = None
    for _, gray in channel_variants(image):
        for _, mask in mask_variants(gray, thresholds=(105, 135, 165)):
            for segments in segment_candidates(mask, max_candidates=max_candidates):
                widths = [part.width for part in segments]
                score = sum(abs(width - sum(widths) / 5) for width in widths)
                if best is None or score < best[0]:
                    best = (score, segments)
    return [] if best is None else [_feature(part) for part in best[1]]


def feature_for_named_variant(image: Image.Image, name: str, *, max_candidates: int) -> list[list[float]]:
    """Extract features for one named channel and all of its threshold masks."""
    selected = [gray for variant, gray in channel_variants(image) if variant == name]
    if not selected:
        raise ValueError(f"unknown variant: {name}")
    best: tuple[float, list[Image.Image]] | None = None
    for gray in selected:
        for _, mask in mask_variants(gray, thresholds=(105, 135, 165)):
            for segments in segment_candidates(mask, max_candidates=max_candidates):
                widths = [part.width for part in segments]
                score = sum(abs(width - sum(widths) / 5) for width in widths)
                if best is None or score < best[0]:
                    best = (score, segments)
    return [] if best is None else [_feature(part) for part in best[1]]


def distance(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)) / max(1, len(left)))


def train_templates(rows: Iterable[dict[str, str]], data_dir: Path, variant: str, max_candidates: int) -> dict[tuple[int, str], list[list[float]]]:
    templates: dict[tuple[int, str], list[list[float]]] = {}
    for row in rows:
        features = feature_for_named_variant(Image.open(data_dir / row["filename"]), variant, max_candidates=max_candidates)
        if len(features) != CELL_COUNT:
            continue
        for position, (digit, feature) in enumerate(zip(row["answer"], features)):
            templates.setdefault((position, digit), []).append(feature)
    return templates


def predict(features: list[list[float]], templates: dict[tuple[int, str], list[list[float]]]) -> str:
    if len(features) != CELL_COUNT:
        return ""
    output = []
    for position, feature in enumerate(features):
        ranked = []
        for digit in "0123456789":
            candidates = templates.get((position, digit), [])
            if candidates:
                ranked.append((min(distance(feature, item) for item in candidates), digit))
        if not ranked:
            return ""
        output.append(min(ranked)[1])
    return "".join(output)


def evaluate_variant(rows: list[dict[str, str]], data_dir: Path, variant: str, max_candidates: int) -> dict[str, object]:
    templates = train_templates(rows, data_dir, variant, max_candidates)
    return {"variant": variant, "training_sources": len({key for values in templates.values() for key in [id(values)]}), **evaluate_rows(rows, data_dir, templates, variant, max_candidates)}


def evaluate_rows(rows: list[dict[str, str]], data_dir: Path, templates: dict[tuple[int, str], list[list[float]]], variant: str, max_candidates: int) -> dict[str, object]:
    exact = edits = characters = abstentions = 0
    errors = []
    for row in rows:
        features = feature_for_named_variant(Image.open(data_dir / row["filename"]), variant, max_candidates=max_candidates)
        predicted = predict(features, templates)
        if len(predicted) != CELL_COUNT:
            abstentions += 1
        exact += int(predicted == row["answer"])
        edits += edit_distance(row["answer"], predicted)
        characters += CELL_COUNT
        if predicted != row["answer"] and len(errors) < 5:
            errors.append({"filename": row["filename"], "expected": row["answer"], "predicted": predicted})
    n = len(rows)
    return {"sources": n, "exact_sequence_accuracy": exact / max(1, n), "exact_sequences": exact,
            "digit_accuracy": 1 - edits / max(1, characters), "character_error_rate": edits / max(1, characters),
            "abstentions": abstentions, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--data", type=Path, default=root / "samples")
    parser.add_argument("--report", type=Path, default=root / "type1_rgb_report.json")
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--max-candidates", type=int, default=6)
    parser.add_argument("--variants", nargs="*", help="Optional subset of channel variant names")
    args = parser.parse_args()
    if args.max_candidates < 1 or args.max_candidates > 18:
        parser.error("--max-candidates must be between 1 and 18")
    try:
        rows = read_dataset(args.data)
        train_rows, validation_rows, split_kind = split_rows(rows, args.validation_fraction, args.seed)
        names = [name for name, _ in channel_variants(load_image(args.data / rows[0]["filename"]))]
        if args.variants:
            unknown = sorted(set(args.variants) - set(names))
            if unknown:
                parser.error(f"unknown variants: {', '.join(unknown)}")
            names = [name for name in names if name in args.variants]
        results = []
        for index, variant in enumerate(names, 1):
            print(f"[{index}/{len(names)}] evaluating {variant}", flush=True)
            templates = train_templates(train_rows, args.data, variant, args.max_candidates)
            metrics = evaluate_rows(validation_rows, args.data, templates, variant, args.max_candidates)
            results.append({"variant": variant, "template_classes": len(templates), **metrics})
    except (OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    results.sort(key=lambda item: (float(item["exact_sequence_accuracy"]), float(item["digit_accuracy"])), reverse=True)
    report = {"data_sources": len(rows), "training_sources": len(train_rows), "validation_sources": len(validation_rows),
              "split": split_kind, "batch_ids_present": all(row.get("batch_id") for row in rows),
              "selection_rule": "winner is reported only; no variant is activated automatically",
              "thresholds": [105, 135, 165], "max_candidates": args.max_candidates, "results": results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
