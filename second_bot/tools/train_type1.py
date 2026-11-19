#!/usr/bin/env python3
"""Train the Type1 classifier and gate promotion against the original benchmark."""
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
except ImportError:
    from type1_ocr import build_model, recognize_type1, save_model
    from type1_ocr.types import CELL_COUNT


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    with (data_dir / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("label.csv must contain filename,type,answer")
    selected, names, hashes = [], set(), {}
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
        names.add(name); hashes[digest] = name
        selected.append({"filename": name, "answer": answer, "batch_id": row.get("batch_id", "").strip(), "type": "type1"})
    if not selected:
        raise ValueError("no type1 rows found")
    return selected


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


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
            names = {row["filename"] for row in holdout}
            return [row for row in rows if row["filename"] not in names], holdout, "batch_holdout"
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    count = max(1, min(len(rows) - 1, round(len(rows) * fraction)))
    return shuffled[count:], shuffled[:count], "random_image_holdout_no_batch_id"


def evaluate(rows: list[dict[str, str]], data_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    results, exact, correct, total, abstentions = [], 0, 0, 0, 0
    for row in rows:
        result = recognize_type1(data_dir / row["filename"], model)
        predicted = result.text
        exact += int(predicted == row["answer"])
        correct += sum(a == b for a, b in zip(predicted, row["answer"]))
        total += CELL_COUNT
        abstentions += int(result.abstained)
        results.append({"filename": row["filename"], "expected": row["answer"], "predicted": predicted, "confidence": result.confidence, "digit_confidence": list(result.digit_confidence), "abstained": result.abstained, "reason": result.reason})
    return {"sources": len(rows), "exact_sequences": exact, "exact_sequence_accuracy": exact / max(1, len(rows)), "correct_digits": correct, "total_digits": total, "digit_accuracy": correct / max(1, total), "abstentions": abstentions, "per_digit_confidence": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    package_dir = Path(__file__).resolve().parents[1]
    parser.add_argument("--data", type=Path, default=root / "samples")
    parser.add_argument("--output", type=Path, default=package_dir / "models" / "type1_model.joblib")
    parser.add_argument("--report", type=Path, default=package_dir / "type1_benchmark.json")
    parser.add_argument("--baseline", type=Path, default=root / "baseline_type1_benchmark.json")
    args = parser.parse_args(argv)
    rows = read_dataset(args.data)
    model = build_model(rows, args.data)
    after = evaluate(rows, args.data, model)
    before = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.is_file() else None
    gate_passed = before is None or (after["exact_sequence_accuracy"] >= before["exact_sequence_accuracy"] and after["digit_accuracy"] >= before["digit_accuracy"])
    report = {"before": {k: before.get(k) for k in ("sources", "exact_sequences", "exact_sequence_accuracy", "digit_accuracy", "abstentions")} if before else None, "after": {k: after[k] for k in ("sources", "exact_sequences", "exact_sequence_accuracy", "digit_accuracy", "abstentions")}, "model": {"classifier_kind": model.get("classifier_kind"), "training_sources": model.get("training_sources"), "sample_count": model.get("sample_count")}, "gate_passed": gate_passed, "pillow_version": PILLOW_VERSION, "model_size_bytes": None, "per_digit_confidence": after["per_digit_confidence"]}
    if not gate_passed:
        report["gate_reason"] = "Refused promotion because accuracy is lower than the original baseline."
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    save_model(model, args.output)
    report["model_size_bytes"] = args.output.stat().st_size
    report["model_under_500kb"] = report["model_size_bytes"] < 500 * 1024
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_digit_confidence"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
