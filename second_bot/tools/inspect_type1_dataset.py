#!/usr/bin/env python3
"""Inspect label.csv and image metadata without mixing Type2 into OCR training."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from .train_type1 import read_dataset
except ImportError:
    from tools.train_type1 import read_dataset


def inspect(data_dir: Path) -> dict[str, object]:
    rows = read_dataset(data_dir)
    formats: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    records = []
    for row in rows:
        path = data_dir / row["filename"]
        with Image.open(path) as image:
            fmt = (image.format or path.suffix.lstrip(".")).upper()
            formats[fmt] += 1
            sizes[f"{image.width}x{image.height}"] += 1
            records.append({"filename": row["filename"], "format": fmt, "size": [image.width, image.height], "answer_length": len(row["answer"])})
    return {
        "type1_count": len(rows),
        "type2_included_in_training": False,
        "formats": dict(sorted(formats.items())),
        "sizes": dict(sorted(sizes.items())),
        "common_size": "300x110",
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[2] / "samples")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args(argv)
    result = inspect(args.data)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
