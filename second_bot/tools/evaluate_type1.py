#!/usr/bin/env python3
"""Evaluate a Type1 candidate on a deterministic holdout without activation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from ..type1_ocr import build_model
    from .train_type1 import evaluate, read_dataset, split_rows
except ImportError:  # direct execution with PYTHONPATH=second_bot
    from type1_ocr import build_model
    from tools.train_type1 import evaluate, read_dataset, split_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--data", type=Path, default=root / "samples")
    parser.add_argument("--report", type=Path, default=Path(__file__).resolve().parents[1] / "type1_evaluation.json")
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args(argv)
    try:
        rows = read_dataset(args.data)
        train, holdout, split_kind = split_rows(rows, args.validation_fraction, args.seed)
        model = build_model(train, args.data)
        metrics = evaluate(holdout, args.data, model)
    except (OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    report = {
        "data_type1_sources": len(rows),
        "type2_excluded": True,
        "training_sources": len(train),
        "validation_sources": len(holdout),
        "split": split_kind,
        "metrics": metrics,
        "selection_rule": "metrics are diagnostic only; no model is activated automatically",
        "production_enabled": False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
