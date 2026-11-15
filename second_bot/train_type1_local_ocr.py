#!/usr/bin/env python3
"""Train/evaluate the Pillow-only type1 local OCR.

The command creates a deterministic validation holdout before feature extraction.
If batch_id is absent, the report explicitly calls the split a random image
holdout.  No validation/test image is used to build templates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

from PIL import Image, __version__ as PILLOW_VERSION

from type1_local_ocr import CELL_COUNT, build_model, recognize


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    with (data_dir / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("label.csv must contain filename,type,answer")
    selected, names, hashes = [], set(), {}
    for row in rows:
        if row.get("type", "").strip() != "type1":
            continue
        name, answer = row.get("filename", "").strip(), row.get("answer", "").strip()
        path = Path(name)
        if not name or path.is_absolute() or len(path.parts) != 1:
            raise ValueError(f"invalid image filename: {name!r}")
        if len(answer) != CELL_COUNT or not answer.isascii() or not answer.isdigit():
            raise ValueError(f"expected five ASCII digits for {name}")
        image_path = data_dir / name
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if name in names or digest in hashes:
            raise ValueError(f"duplicate filename/content: {name}")
        names.add(name); hashes[digest] = name
        selected.append({"filename": name, "answer": answer, "batch_id": row.get("batch_id", "").strip(), "sha256": digest})
    if not selected:
        raise ValueError("no type1 rows found")
    return selected


def split_rows(rows: list[dict[str, str]], fraction: float, seed: int) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    if not 0.05 <= fraction <= 0.4:
        raise ValueError("validation fraction must be between 0.05 and 0.4")
    groups: dict[str, list[dict[str, str]]] = {}
    if all(row.get("batch_id") for row in rows):
        for row in rows:
            groups.setdefault(row["batch_id"], []).append(row)
        if len(groups) >= 2:
            target = max(1, round(len(rows) * fraction))
            holdout = min(groups.values(), key=lambda group: abs(len(group) - target))
            holdout_names = {row["filename"] for row in holdout}
            return [r for r in rows if r["filename"] not in holdout_names], holdout, "batch_holdout"
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    count = max(1, min(len(rows) - 1, round(len(rows) * fraction)))
    return shuffled[count:], shuffled[:count], "random_image_holdout_no_batch_id"


def edit_distance(left: str, right: str) -> int:
    prev = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        cur = [i]
        for j, b in enumerate(right, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[-1]


def evaluate(rows: list[dict[str, str]], data_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    exact = chars = edits = abstained = 0
    errors = []
    for row in rows:
        result = recognize(data_dir / row["filename"], model)
        predicted = result.get("text", "")
        if result.get("abstained"): abstained += 1
        exact += int(predicted == row["answer"])
        edits += edit_distance(row["answer"], predicted)
        chars += len(row["answer"])
        if predicted != row["answer"] and len(errors) < 20:
            errors.append({"filename": row["filename"], "expected": row["answer"], "predicted": predicted})
    n = len(rows)
    return {"sources": n, "exact_sequence_accuracy": exact / max(1, n), "exact_sequences": exact,
            "digit_accuracy": 1 - edits / max(1, chars), "character_error_rate": edits / max(1, chars),
            "abstentions": abstained, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--data", type=Path, default=root / "samples")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "models" / "type1_local_ocr.json")
    parser.add_argument("--report", type=Path, default=root / "type1_local_ocr_report.json")
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()
    try:
        rows = read_dataset(args.data)
        train, validation, split_kind = split_rows(rows, args.validation_fraction, args.seed)
        model = build_model(train, args.data)
        metrics = evaluate(validation, args.data, model)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"dataset error: {exc}", file=sys.stderr); return 2
    report = {"baseline": {"source": "existing repository README", "exact_sequence_accuracy": 0.0, "character_accuracy": 0.275, "sample_count": 170},
              "new_model": metrics, "training_sources": len(train), "validation_sources": len(validation),
              "split": split_kind, "batch_ids_present": all(r.get("batch_id") for r in rows),
              "pillow_version": PILLOW_VERSION, "python_version": sys.version.split()[0],
              "processing": {"masks": ["global_105", "global_135", "global_165", "background_delta"], "max_split_candidates": 18, "feature": "normalized mask + horizontal/vertical projections"},
              "activated_in_bot": False, "deployment_reason": "No separate untouched test batch is available; keep type1 production path unchanged."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
