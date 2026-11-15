#!/usr/bin/env python3
"""Train a Type1-only candidate model and report a held-out evaluation.

The candidate output is deliberately outside the bot's production model path.
No training result is enabled automatically.
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

from PIL import __version__ as PILLOW_VERSION

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from ..type1_ocr import build_model, recognize_type1, save_model
    from ..type1_ocr.types import CELL_COUNT
except ImportError:  # direct execution with PYTHONPATH=second_bot
    from type1_ocr import build_model, recognize_type1, save_model
    from type1_ocr.types import CELL_COUNT


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    """Read only Type1 labels; validate filenames, labels, existence, and duplicates."""
    with (data_dir / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("label.csv must contain filename,type,answer")
    selected: list[dict[str, str]] = []
    names: set[str] = set()
    hashes: dict[str, str] = {}
    for row in rows:
        if row.get("type", "").strip().lower() != "type1":
            continue
        name, answer = row.get("filename", "").strip(), row.get("answer", "").strip()
        path = Path(name)
        if not name or path.is_absolute() or len(path.parts) != 1 or name in {".", ".."}:
            raise ValueError(f"invalid image filename: {name!r}")
        if len(answer) != CELL_COUNT or not answer.isascii() or not answer.isdigit():
            raise ValueError(f"expected five ASCII digits for {name}")
        image_path = data_dir / name
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if name in names or digest in hashes:
            raise ValueError(f"duplicate filename/content: {name}")
        names.add(name)
        hashes[digest] = name
        selected.append({"filename": name, "answer": answer, "batch_id": row.get("batch_id", "").strip(), "sha256": digest, "type": "type1"})
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
            return [row for row in rows if row["filename"] not in holdout_names], holdout, "batch_holdout"
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    count = max(1, min(len(rows) - 1, round(len(rows) * fraction)))
    return shuffled[count:], shuffled[:count], "random_image_holdout_no_batch_id"


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def evaluate(rows: list[dict[str, str]], data_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    exact = edits = characters = abstained = 0
    errors = []
    for row in rows:
        result = recognize_type1(data_dir / row["filename"], model)
        predicted = result.text
        abstained += int(result.abstained)
        exact += int(predicted == row["answer"])
        edits += edit_distance(row["answer"], predicted)
        characters += CELL_COUNT
        if predicted != row["answer"] and len(errors) < 20:
            errors.append({"filename": row["filename"], "expected": row["answer"], "predicted": predicted, "confidence": result.confidence, "reason": result.reason})
    count = len(rows)
    return {
        "sources": count,
        "exact_sequence_accuracy": exact / max(1, count),
        "exact_sequences": exact,
        "digit_accuracy": 1 - edits / max(1, characters),
        "character_error_rate": edits / max(1, characters),
        "abstentions": abstained,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    package_dir = Path(__file__).resolve().parents[1]
    parser.add_argument("--data", type=Path, default=root / "samples")
    parser.add_argument("--output", type=Path, default=package_dir / "models" / "candidates" / "type1_local_ocr_candidate.json", help="candidate path; does not enable production OCR")
    parser.add_argument("--report", type=Path, default=package_dir / "type1_local_ocr_report.json")
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args(argv)
    production_model = (package_dir / "models" / "type1_local_ocr.json").resolve()
    if args.output.expanduser().resolve() == production_model:
        parser.error("refusing to overwrite the bot's production model path; train to a candidate file and promote only after independent approval")
    try:
        rows = read_dataset(args.data)
        train, validation, split_kind = split_rows(rows, args.validation_fraction, args.seed)
        model = build_model(train, args.data)
        metrics = evaluate(validation, args.data, model)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    report = {
        "dataset": {"type1_sources": len(rows), "type2_excluded": True, "training_sources": len(train), "validation_sources": len(validation)},
        "new_model": metrics,
        "split": split_kind,
        "batch_ids_present": all(row.get("batch_id") for row in rows),
        "pillow_version": PILLOW_VERSION,
        "python_version": sys.version.split()[0],
        "model_output": str(args.output),
        "production_enabled": False,
        "deployment_gate": {
            "passed": False,
            "reason": "A random image holdout without an independent untouched batch does not establish parity with the current production OCR.Space/Gemini fallback path.",
        },
    }
    save_model(model, args.output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
