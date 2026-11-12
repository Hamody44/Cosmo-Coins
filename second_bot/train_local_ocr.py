#!/usr/bin/env python3
"""Train and evaluate the lightweight local OCR prototypes.

Usage from second_bot/: python3 train_local_ocr.py
The script performs leave-one-image-out validation for both sequence-level
nearest-neighbor retrieval and character-level HOG classification, then writes
an experimental template model. It does not install or require ddddocr.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from local_ocr import (
    SETTINGS,
    TYPE1_PREPROCESSING_VARIANTS,
    digit_features,
    full_image_feature,
    load_image,
    predict_digit_features,
)

DIGITS = set("0123456789")


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    label_path = data_dir / "label.csv"
    with label_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"filename", "type", "answer"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{label_path} must have columns {sorted(required)}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"No labels found in {label_path}")
    seen = set()
    for row in rows:
        name, style, answer = row["filename"], row["type"], row["answer"]
        if name in seen:
            raise ValueError(f"Duplicate filename in labels: {name}")
        seen.add(name)
        if style not in SETTINGS:
            raise ValueError(f"Unsupported style {style!r} in row {name}")
        if len(answer) != 5 or not set(answer).issubset(DIGITS):
            raise ValueError(f"Expected exactly five digits for {name}: {answer!r}")
        if not (data_dir / name).is_file():
            raise FileNotFoundError(data_dir / name)
    return rows


def prepare_rows(rows: list[dict[str, str]], data_dir: Path) -> list[dict[str, Any]]:
    prepared = []
    for row in rows:
        image = load_image(data_dir / row["filename"])
        style = row["type"]
        if style != row["type"]:
            raise ValueError(f"Style mismatch for {row['filename']}")
        prepared.append(
            {
                **row,
                "char_features": digit_features(image, style),
                "whole_feature": full_image_feature(image, style),
            }
        )
    return prepared


def make_templates(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    templates = []
    for row in prepared:
        for position, (digit, feature) in enumerate(zip(row["answer"], row["char_features"])):
            templates.append(
                {
                    "type": row["type"],
                    "filename": row["filename"],
                    "position": position,
                    "digit": digit,
                    "feature": feature,
                }
            )
    return templates


def evaluate_character_level(prepared: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for style in sorted({row["type"] for row in prepared}):
        subset = [row for row in prepared if row["type"] == style]
        templates = make_templates(subset)
        correct_digits = total_digits = exact_sequences = 0
        errors = []
        for row in subset:
            available = [t for t in templates if t["filename"] != row["filename"]]
            prediction = predict_digit_features(row["char_features"], available)
            guess = prediction["text"]
            correct_digits += sum(a == b for a, b in zip(guess, row["answer"]))
            total_digits += len(row["answer"])
            if guess == row["answer"]:
                exact_sequences += 1
            else:
                errors.append({"filename": row["filename"], "expected": row["answer"], "predicted": guess})
        result[style] = {
            "method": "character HOG nearest-template; all five positions pooled; leave-one-image-out",
            "digit_accuracy": correct_digits / total_digits,
            "exact_sequence_accuracy": exact_sequences / len(subset),
            "correct_sequences": exact_sequences,
            "total_sequences": len(subset),
            "errors": errors,
        }
    return result


def evaluate_type1_preprocessing(rows: list[dict[str, str]], data_dir: Path) -> dict[str, Any]:
    """Benchmark optional type1 preprocessing without changing the default path."""
    type1_rows = [row for row in rows if row["type"] == "type1"]
    results: dict[str, Any] = {}
    for variant in TYPE1_PREPROCESSING_VARIANTS:
        prepared = [
            {
                **row,
                "char_features": digit_features(
                    load_image(data_dir / row["filename"]), "type1", variant=variant
                ),
            }
            for row in type1_rows
        ]
        templates = make_templates(prepared)
        correct_digits = exact_sequences = 0
        for row in prepared:
            available = [t for t in templates if t["filename"] != row["filename"]]
            guess = predict_digit_features(row["char_features"], available)["text"]
            correct_digits += sum(a == b for a, b in zip(guess, row["answer"]))
            exact_sequences += guess == row["answer"]
        results[variant] = {
            "digit_accuracy": correct_digits / max(1, len(type1_rows) * 5),
            "exact_sequence_accuracy": exact_sequences / max(1, len(type1_rows)),
        }
    return results


def hamming_distance(left: list[int], right: list[int]) -> float:
    if len(left) != len(right):
        raise ValueError("Full-image feature size mismatch")
    return sum(a != b for a, b in zip(left, right)) / max(1, len(left))


def evaluate_full_sequence(prepared: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Benchmark whole-number nearest-neighbor retrieval, not novel-code OCR."""
    result = {}
    for style in sorted({row["type"] for row in prepared}):
        subset = [row for row in prepared if row["type"] == style]
        correct = 0
        errors = []
        for row in subset:
            candidates = [other for other in subset if other["filename"] != row["filename"]]
            nearest = min(candidates, key=lambda other: hamming_distance(row["whole_feature"], other["whole_feature"]))
            guess = nearest["answer"]
            correct += guess == row["answer"]
            if guess != row["answer"]:
                errors.append({"filename": row["filename"], "expected": row["answer"], "predicted": guess})
        result[style] = {
            "method": "whole-image nearest stored sequence; leave-one-image-out",
            "exact_sequence_accuracy": correct / len(subset),
            "correct_sequences": correct,
            "total_sequences": len(subset),
            "unseen_sequences_supported": False,
            "errors": errors,
        }
    return result


def build_model(prepared: list[dict[str, Any]], validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_name": "cosmo-local-hog-character-templates",
        "experimental": True,
        "input_size": [300, 110],
        "style_settings": SETTINGS,
        "training_count": {
            style: sum(row["type"] == style for row in prepared)
            for style in sorted({row["type"] for row in prepared})
        },
        "validation": validation,
        "templates": make_templates(prepared),
        # Kept only for exact/near-duplicate lookup experiments. Do not use this
        # table as the answer source for a novel sequence.
        "whole_sequence_templates": [
            {
                "type": row["type"],
                "filename": row["filename"],
                "answer": row["answer"],
                "feature": row["whole_feature"],
            }
            for row in prepared
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent.parent / "captcha_samples")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "models" / "local_ocr_model.json")
    parser.add_argument("--evaluate-only", action="store_true", help="Run both evaluations without writing a model")
    args = parser.parse_args()

    try:
        rows = read_dataset(args.data)
        prepared = prepare_rows(rows, args.data)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    validation = {
        "character_level": evaluate_character_level(prepared),
        "full_sequence": evaluate_full_sequence(prepared),
        "type1_preprocessing": evaluate_type1_preprocessing(rows, args.data),
    }
    print(json.dumps(validation, ensure_ascii=False, indent=2))

    if args.evaluate_only:
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model = build_model(prepared, validation)
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(model, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Experimental model written to {args.output}")
    print("The model is not automatically enabled in the bot; inspect validation first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
