#!/usr/bin/env python3
"""Train and evaluate the independent Pillow-only type1 OCR model.

Run from second_bot/:
    python3 train_type1_local_ocr.py --evaluate-only
    python3 train_type1_local_ocr.py
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from type1_local_ocr import PREPROCESSING_VARIANTS, extract_features, predict_features
from type1_local_ocr import _crop_cell, load_image, make_masks, normalize_mask

DIGITS = set("0123456789")


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    label_path = data_dir / "label.csv"
    with label_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{label_path} must contain {sorted(required)}")
    selected = []
    seen = set()
    for row in rows:
        if row["type"] != "type1":
            continue
        name, answer = row["filename"], row["answer"]
        if name in seen:
            raise ValueError(f"Duplicate filename: {name}")
        if len(answer) != 5 or not set(answer).issubset(DIGITS):
            raise ValueError(f"Expected five digits for {name}: {answer!r}")
        image_path = data_dir / name
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        seen.add(name)
        selected.append({"filename": name, "type": "type1", "answer": answer})
    if not selected:
        raise ValueError("No type1 rows found")
    return selected


def prepare_rows(rows: list[dict[str, str]], data_dir: Path) -> list[dict[str, Any]]:
    prepared = []
    for row in rows:
        prepared.append({**row, "features": extract_features(data_dir / row["filename"])})
    return prepared


def make_templates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    templates = []
    for row in rows:
        for variant in PREPROCESSING_VARIANTS:
            for position, (digit, feature) in enumerate(zip(row["answer"], row["features"][variant])):
                templates.append({
                    "filename": row["filename"],
                    "variant": variant,
                    "position": position,
                    "digit": digit,
                    "feature": feature,
                })
    return templates


def evaluate(
    rows: list[dict[str, Any]], *, class_weight_alpha: float = 0.0
) -> dict[str, Any]:
    correct_digits = 0
    total_digits = len(rows) * 5
    correct_sequences = 0
    errors = []
    for row in rows:
        templates = make_templates([other for other in rows if other["filename"] != row["filename"]])
        result = predict_features(
            row["features"],
            templates,
            exclude_filename=row["filename"],
            class_weight_alpha=class_weight_alpha,
        )
        guess = result.get("text", "")
        correct_digits += sum(left == right for left, right in zip(guess, row["answer"]))
        if guess == row["answer"]:
            correct_sequences += 1
        else:
            errors.append({
                "filename": row["filename"],
                "expected": row["answer"],
                "predicted": guess,
                "reliable": result.get("reliable", False),
            })
    return {
        "method": (
            "position-aware multi-mask KNN with leave-one-image-out validation; "
            f"class_weight_alpha={class_weight_alpha}"
        ),
        "digit_accuracy": correct_digits / max(1, total_digits),
        "exact_sequence_accuracy": correct_sequences / max(1, len(rows)),
        "correct_sequences": correct_sequences,
        "total_sequences": len(rows),
        "errors": errors,
    }


def build_model(
    rows: list[dict[str, Any]],
    validation: dict[str, Any],
    *,
    class_weight_alpha: float = 0.0,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_name": "cosmo-local-type1-multimask-knn",
        "input_size": [300, 110],
        "preprocessing_variants": list(PREPROCESSING_VARIANTS),
        "training_count": len(rows),
        "class_weight_alpha": class_weight_alpha,
        "validation": validation,
        "templates": make_templates(rows),
    }


def save_debug_images(
    rows: list[dict[str, str]],
    data_dir: Path,
    output_dir: Path,
    *,
    limit: int,
) -> int:
    """Save source, masks, and five digit crops for visual preprocessing review."""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = rows[: max(0, limit)]
    for row in selected:
        stem = Path(row["filename"]).stem
        image_dir = output_dir / stem
        image_dir.mkdir(parents=True, exist_ok=True)
        image = load_image(data_dir / row["filename"])
        image.save(image_dir / "00_source.png")
        masks = make_masks(image)
        for variant in PREPROCESSING_VARIANTS:
            mask = masks[variant]
            mask.save(image_dir / f"01_mask_{variant}.png")
            crops = Image.new("L", (5 * 80, 120), 0)
            for position in range(5):
                crop = normalize_mask(_crop_cell(mask, position)).resize((64, 96))
                crops.paste(crop, (position * 80 + 8, 12))
            crops.convert("RGB").save(image_dir / f"02_crops_{variant}.png")

        # A compact contact sheet makes it easy to compare all masks at once.
        sheet = Image.new("RGB", (600, 390), "white")
        draw = ImageDraw.Draw(sheet)
        panels = [("source", image)] + [(variant, masks[variant]) for variant in PREPROCESSING_VARIANTS]
        for index, (label, panel) in enumerate(panels):
            x = (index % 2) * 300
            y = (index // 2) * 130
            sheet.paste(panel.convert("RGB").resize((300, 110)), (x, y + 20))
            draw.text((x + 5, y + 3), label, fill="black")
        sheet.save(image_dir / "03_contact_sheet.png")
    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent.parent / "captcha_samples")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "models" / "type1_local_ocr_model.json")
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument(
        "--class-weight-alpha",
        type=float,
        default=0.0,
        help="Apply position-wise inverse-frequency class weights (default: 0, disabled)",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        help="Save source/mask/crop diagnostics for a limited number of type1 images",
    )
    parser.add_argument(
        "--debug-limit",
        type=int,
        default=10,
        help="Maximum number of images to export with --debug-dir (default: 10)",
    )
    args = parser.parse_args()
    if not math.isfinite(args.class_weight_alpha) or args.class_weight_alpha < 0:
        parser.error("--class-weight-alpha must be a finite, non-negative number")
    try:
        rows = read_dataset(args.data)
        prepared = prepare_rows(rows, args.data)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2
    if args.debug_dir is not None:
        count = save_debug_images(rows, args.data, args.debug_dir, limit=args.debug_limit)
        print(f"Saved preprocessing diagnostics for {count} image(s) to {args.debug_dir}")
    validation = evaluate(prepared, class_weight_alpha=args.class_weight_alpha)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if args.evaluate_only:
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            build_model(
                prepared,
                validation,
                class_weight_alpha=args.class_weight_alpha,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(f"Model written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
