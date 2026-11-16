#!/usr/bin/env python3
"""Train the lightweight NumPy Type1 template bank from labelled images.

Pillow is used only by this offline training command to decode the repository's
WebP/PNG/JPG samples. The generated model and production recognizer use only
NumPy and the standard library.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from type1 import (
    build_template_bank,
    extract_fixed_digits,
    extract_shifted_digits,
    match_position,
    normalize_glyph,
    save_template_bank,
)


def read_rows(root: Path) -> list[dict[str, str]]:
    with (root / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("type", "").lower() == "type1"]


def load_images(root: Path, rows: list[dict[str, str]]) -> dict[str, np.ndarray]:
    return {
        row["filename"]: np.asarray(Image.open(root / row["filename"]).convert("L"), dtype=np.uint8)
        for row in rows
    }


def fixed_glyphs(gray: np.ndarray, thresholds: tuple[int, ...], margin: float) -> list[np.ndarray]:
    height, width = gray.shape
    left, right = round(width * margin), round(width * (1.0 - margin))
    output = []
    for position in range(5):
        start = left + (right - left) * position // 5
        end = left + (right - left) * (position + 1) // 5
        output.append((gray[:, start:end] <= thresholds[position]).copy())
    return output


def make_bank(rows, images, thresholds, margin):
    glyphs = [fixed_glyphs(images[row["filename"]], thresholds, margin) for row in rows]
    return build_template_bank(glyphs, [row["answer"] for row in rows])


def compact_bank(bank, limit: int = 2):
    """Keep a small representative search bank while tuning hyperparameters."""
    return [{digit: values[:limit] for digit, values in position.items()} for position in bank]


def predict(gray, bank, thresholds, margin, local_shift):
    if local_shift:
        glyphs = extract_shifted_digits(gray, thresholds, bank, margin=margin, local_shift=local_shift)
    else:
        glyphs = fixed_glyphs(gray, thresholds, margin)
    return "".join(
        match_position(normalize_glyph(glyph), bank[position])[0]
        for position, glyph in enumerate(glyphs)
    )


def evaluate(rows, images, bank, thresholds, margin, local_shift) -> tuple[int, int]:
    exact = correct = 0
    for row in rows:
        predicted = predict(images[row["filename"]], bank, thresholds, margin, local_shift)
        exact += int(predicted == row["answer"])
        correct += sum(left == right for left, right in zip(predicted, row["answer"]))
    return exact, correct


def tune_thresholds(rows, images, margin, candidates, seed):
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    inner_size = max(20, round(len(shuffled) * 0.20))
    inner_validation, inner_training = shuffled[:inner_size], shuffled[inner_size:]
    selected = [192] * 5
    trace = []
    for position in range(5):
        scores = []
        for candidate in candidates:
            trial = tuple(selected[:position] + [candidate] + selected[position + 1 :])
            bank = compact_bank(make_bank(inner_training, images, trial, margin))
            exact, correct = evaluate(inner_validation, images, bank, trial, margin, 0)
            scores.append({"threshold": candidate, "exact": exact, "correct": correct})
        best = max(scores, key=lambda item: (item["correct"], item["exact"], -abs(item["threshold"] - 192)))
        selected[position] = best["threshold"]
        trace.append({"position": position, "candidates": scores, "selected": best["threshold"]})
    return tuple(selected), inner_training, inner_validation, trace


def tune_shift(inner_training, inner_validation, images, thresholds, margin, candidates):
    bank = compact_bank(make_bank(inner_training, images, thresholds, margin))
    scores = []
    for shift in candidates:
        exact, correct = evaluate(inner_validation, images, bank, thresholds, margin, shift)
        scores.append({"local_shift": shift, "exact": exact, "correct": correct})
    best = max(scores, key=lambda item: (item["correct"], item["exact"], -item["local_shift"]))
    return best["local_shift"], scores


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root_default = Path(__file__).resolve().parents[2]
    parser.add_argument("--data", type=Path, default=root_default / "samples")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "type1_templates.npz")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "type1_templates_report.json")
    parser.add_argument("--margin", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()

    rows = read_rows(args.data)
    if not rows:
        raise SystemExit("no type1 rows found")
    images = load_images(args.data, rows)
    shuffled = list(rows)
    random.Random(args.seed).shuffle(shuffled)
    holdout_size = max(1, round(len(rows) * 0.20))
    validation, training = shuffled[:holdout_size], shuffled[holdout_size:]

    thresholds, inner_training, inner_validation, threshold_trace = tune_thresholds(
        training, images, args.margin, (160, 192, 224), args.seed + 1
    )
    local_shift, shift_trace = tune_shift(
        inner_training, inner_validation, images, thresholds, args.margin, (0, 2, 4, 6, 8)
    )
    validation_bank = make_bank(training, images, thresholds, args.margin)
    validation_exact, validation_correct = evaluate(
        validation, images, validation_bank, thresholds, args.margin, local_shift
    )
    full_bank = make_bank(rows, images, thresholds, args.margin)
    save_template_bank(full_bank, args.output)
    full_exact, full_correct = evaluate(rows, images, full_bank, thresholds, args.margin, local_shift)
    report = {
        "sources": len(rows),
        "training_sources": len(training),
        "validation_sources": len(validation),
        "inner_training_sources": len(inner_training),
        "inner_validation_sources": len(inner_validation),
        "position_thresholds": list(thresholds),
        "margin": args.margin,
        "local_shift": local_shift,
        "template_shape": [32, 24],
        "validation_exact_accuracy": validation_exact / len(validation),
        "validation_digit_accuracy": validation_correct / (len(validation) * 5),
        "full_dataset_exact_accuracy": full_exact / len(rows),
        "full_dataset_digit_accuracy": full_correct / (len(rows) * 5),
        "threshold_tuning": threshold_trace,
        "shift_tuning": shift_trace,
        "model": str(args.output),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
