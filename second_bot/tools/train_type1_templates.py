#!/usr/bin/env python3
"""Train the lightweight NumPy Type1 template bank from labelled images.

Pillow is used only by this offline training command to decode the repository's
WebP/PNG/JPG samples.  The generated model and production recognizer use only
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
    match_position,
    normalize_glyph,
    save_template_bank,
)


def read_rows(root: Path) -> list[dict[str, str]]:
    with (root / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("type", "").lower() == "type1"]


def load_glyphs(root: Path, row: dict[str, str], threshold: int, margin: float) -> list[np.ndarray]:
    gray = np.asarray(Image.open(root / row["filename"]).convert("L"), dtype=np.uint8)
    binary = gray <= threshold
    return extract_fixed_digits(binary, count=5, margin=margin)


def evaluate(rows, glyph_cache, bank) -> tuple[int, int]:
    exact = correct = 0
    for row in rows:
        predicted = "".join(
            match_position(normalize_glyph(glyph), bank[position])[0]
            for position, glyph in enumerate(glyph_cache[row["filename"]])
        )
        exact += int(predicted == row["answer"])
        correct += sum(left == right for left, right in zip(predicted, row["answer"]))
    return exact, correct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root_default = Path(__file__).resolve().parents[2]
    parser.add_argument("--data", type=Path, default=root_default / "samples")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "type1_templates.npz")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "type1_templates_report.json")
    parser.add_argument("--threshold", type=int, default=192)
    parser.add_argument("--margin", type=float, default=0.10)
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()

    rows = read_rows(args.data)
    if not rows:
        raise SystemExit("no type1 rows found")
    cache = {row["filename"]: load_glyphs(args.data, row, args.threshold, args.margin) for row in rows}
    shuffled = list(rows)
    random.Random(args.seed).shuffle(shuffled)
    holdout_size = max(1, round(len(rows) * args.validation_fraction))
    validation = shuffled[:holdout_size]
    training = shuffled[holdout_size:]
    training_bank = build_template_bank([cache[row["filename"]] for row in training], [row["answer"] for row in training])
    validation_exact, validation_correct = evaluate(validation, cache, training_bank)
    full_bank = build_template_bank([cache[row["filename"]] for row in rows], [row["answer"] for row in rows])
    save_template_bank(full_bank, args.output)
    full_exact, full_correct = evaluate(rows, cache, full_bank)
    report = {
        "sources": len(rows),
        "training_sources": len(training),
        "validation_sources": len(validation),
        "threshold": args.threshold,
        "margin": args.margin,
        "template_shape": [32, 24],
        "templates_per_position_and_digit": "variable bank from real labelled glyphs",
        "validation_exact_accuracy": validation_exact / len(validation),
        "validation_digit_accuracy": validation_correct / (len(validation) * 5),
        "full_dataset_exact_accuracy": full_exact / len(rows),
        "full_dataset_digit_accuracy": full_correct / (len(rows) * 5),
        "model": str(args.output),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
