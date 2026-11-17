#!/usr/bin/env python3
"""Evaluate per-image/per-position adaptive thresholds with bounded Beam Search.

Training uses only the 247-image training split. The 62-image validation split is
held out until final scoring. Offline image decoding uses Pillow; production
recognition remains NumPy/stdlib-only.
"""
from __future__ import annotations
import csv, json, random, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from type1 import build_template_bank, hamming_distance, normalize_glyph, beam_decode

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "samples"
THRESHOLDS = (160, 176, 192, 208, 224)
BASE_THRESHOLDS = (224, 160, 192, 192, 224)
MARGIN = 0.10
BEAM_WIDTH = 10
CANDIDATE_LIMIT = 3


def read_rows():
    with (DATA / "label.csv").open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("type", "").lower() == "type1"]


def load_images(rows):
    return {r["filename"]: np.asarray(Image.open(DATA / r["filename"]).convert("L"), dtype=np.uint8) for r in rows}


def split(rows):
    shuffled = list(rows)
    random.Random(41).shuffle(shuffled)
    size = round(len(rows) * 0.20)
    return shuffled[size:], shuffled[:size]


def crops(gray, thresholds):
    height, width = gray.shape
    left, right = round(width * MARGIN), round(width * (1 - MARGIN))
    return [
        (gray[:, left + (right-left)*p//5:left + (right-left)*(p+1)//5] <= thresholds[p]).copy()
        for p in range(5)
    ]


def make_bank(rows, images, thresholds):
    glyphs = [crops(images[r["filename"]], thresholds) for r in rows]
    return build_template_bank(glyphs, [r["answer"] for r in rows])


def ranked(glyph, templates, limit=CANDIDATE_LIMIT):
    normalized = normalize_glyph(glyph)
    result = [(digit, min(hamming_distance(normalized, t) for t in values))
              for digit, values in templates.items() if values]
    return sorted(result, key=lambda item: (item[1], item[0]))[:limit]


def adaptive_candidates(gray, banks):
    """Return merged per-position candidates, retaining the best threshold evidence."""
    result = []
    diagnostics = []
    for position in range(5):
        by_digit = {}
        by_threshold = {}
        for threshold, bank in zip(THRESHOLDS, banks):
            glyph = crops(gray, tuple(threshold if p == position else BASE_THRESHOLDS[p] for p in range(5)))[position]
            candidates = ranked(glyph, bank[position])
            by_threshold[threshold] = candidates
            for digit, distance in candidates:
                current = by_digit.get(digit)
                item = (distance, threshold)
                if current is None or item < current:
                    by_digit[digit] = item
        merged = sorted((distance, digit) for digit, (distance, _) in by_digit.items())
        result.append([(digit, distance) for distance, digit in merged[:CANDIDATE_LIMIT]])
        diagnostics.append({"position": position, "best_threshold": min(by_digit, key=lambda d: by_digit[d])[0] if by_digit else None, "threshold_candidates": by_threshold})
    return result, diagnostics


def fixed_candidates(gray, bank, thresholds):
    return [[(digit, distance) for digit, distance in ranked(glyph, bank[p])]
            for p, glyph in enumerate(crops(gray, thresholds))]


def evaluate(rows, images, banks, fixed_bank, *, adaptive, exact_errors=False):
    position_correct = [0] * 5
    exact = 0
    low02 = low05 = 0
    errors = []
    for row in rows:
        gray = images[row["filename"]]
        if adaptive:
            candidates, diagnostics = adaptive_candidates(gray, banks)
        else:
            candidates = fixed_candidates(gray, fixed_bank, BASE_THRESHOLDS)
            diagnostics = []
        beam = beam_decode(candidates, beam_width=BEAM_WIDTH)
        predicted, score = beam[0]
        answer = row["answer"]
        exact += predicted == answer
        for p, (actual, expected) in enumerate(zip(predicted, answer)):
            position_correct[p] += actual == expected
        margins = []
        for position in candidates:
            ordered = sorted(distance for _, distance in position)
            margins.append(ordered[1] - ordered[0] if len(ordered) > 1 else 0.0)
        low02 += sum(m < 0.02 for m in margins)
        low05 += sum(m < 0.05 for m in margins)
        if exact_errors and predicted != answer:
            errors.append({"filename": row["filename"], "answer": answer, "predicted": predicted, "wrong_positions": [p for p, (a, b) in enumerate(zip(predicted, answer)) if a != b], "score": score, "margins": margins, "diagnostics": diagnostics})
    n = len(rows)
    return {"exact_accuracy": exact/n, "digit_accuracy": sum(position_correct)/(n*5), "position_accuracy": [x/n for x in position_correct], "position_error": [1-x/n for x in position_correct], "low_margin_002": low02, "low_margin_005": low05, "errors": errors}


def main():
    started = time.time()
    rows = read_rows(); images = load_images(rows); training, validation = split(rows)
    # Adaptive evaluation expects banks aligned by threshold, so build a conservative
    # shared bank set for position=3 and baseline banks for the other positions.
    adaptive_banks = []
    for threshold in THRESHOLDS:
        adaptive_banks.append(make_bank(training, images, tuple(threshold if p == 3 else BASE_THRESHOLDS[p] for p in range(5))))
    fixed_bank = make_bank(training, images, BASE_THRESHOLDS)
    fixed = evaluate(validation, images, adaptive_banks, fixed_bank, adaptive=False, exact_errors=True)
    adaptive = evaluate(validation, images, adaptive_banks, fixed_bank, adaptive=True, exact_errors=True)
    report = {"dataset": {"sources": len(rows), "training_sources": len(training), "validation_sources": len(validation), "seed": 41, "validation_not_used_for_banks": True}, "configuration": {"thresholds": list(THRESHOLDS), "base_thresholds": list(BASE_THRESHOLDS), "beam_width": BEAM_WIDTH, "candidate_limit": CANDIDATE_LIMIT, "margin": MARGIN}, "fixed_baseline": fixed, "adaptive": adaptive, "runtime_seconds": time.time()-started, "credits_estimate": 0}
    output = ROOT / "second_bot" / "models" / "adaptive_threshold_experiment_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"fixed": {k: fixed[k] for k in ("exact_accuracy", "digit_accuracy", "position_accuracy")}, "adaptive": {k: adaptive[k] for k in ("exact_accuracy", "digit_accuracy", "position_accuracy")}, "runtime_seconds": report["runtime_seconds"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
