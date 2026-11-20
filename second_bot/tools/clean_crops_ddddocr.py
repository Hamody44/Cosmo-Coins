#!/usr/bin/env python3
"""Offline ddddocr quality gate for labelled digit crops.

The OCR dependency is intentionally training-only; quarantined files are moved,
never deleted, so a later review can restore them if needed.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
from pathlib import Path

import ddddocr
import numpy as np
from PIL import Image, ImageOps

_DIGITS = "0123456789"
_NORMALIZE = {"o": "0", "O": "0", "i": "1", "I": "1", "l": "1", "L": "1"}


def png_bytes(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()


def ink_stats(image: Image.Image) -> tuple[float, float, float, int]:
    gray = np.asarray(ImageOps.grayscale(image), dtype=np.uint8)
    ink = gray < 210
    if not ink.any():
        return 0.0, 0.0, 0.0, 0
    ys, xs = np.nonzero(ink)
    area = float(ink.mean())
    bbox_area = float((xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)) / max(1, gray.size)
    aspect = float(xs.max() - xs.min() + 1) / max(1, ys.max() - ys.min() + 1)
    # A valid crop has a meaningful central glyph, not a handful of isolated pixels.
    components = np.zeros_like(ink, dtype=np.uint8)
    components[ink] = 1
    # Pillow-only flood fill keeps the cleaner independent of OpenCV.
    seen = np.zeros_like(components, dtype=bool)
    largest = 0
    h, w = components.shape
    for y, x in zip(*np.nonzero(components)):
        if seen[y, x]:
            continue
        stack = [(int(y), int(x))]
        seen[y, x] = True
        count = 0
        while stack:
            cy, cx = stack.pop(); count += 1
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and components[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True; stack.append((ny, nx))
        largest = max(largest, count)
    return area, bbox_area, aspect, largest


def classify(ocr: ddddocr.DdddOcr, image: Image.Image, label: str) -> tuple[bool, str, str]:
    gray = ImageOps.grayscale(image)
    bordered = ImageOps.expand(gray, border=16, fill=255)
    try:
        raw = str(ocr.classification(png_bytes(bordered))).strip()
    except Exception as exc:  # malformed files are quarantined, not fatal to the run
        return False, "", f"ocr_error:{type(exc).__name__}"
    compact = "".join(ch for ch in raw if ch.isalnum())
    predicted = _NORMALIZE.get(compact, compact)
    if predicted == label:
        return True, predicted, "match"
    area, bbox_area, aspect, largest = ink_stats(gray)
    # Empty/ambiguous OCR is retained only for a clearly substantial glyph.
    uncertain = predicted in {"", "?", "-"} or len(predicted) != 1
    clearly_valid = 0.035 <= area <= 0.70 and bbox_area >= 0.08 and 0.12 <= aspect <= 2.5 and largest >= 20
    if uncertain and clearly_valid:
        return True, predicted, "uncertain_but_substantial"
    return False, predicted, "mismatch" if predicted else "empty_or_invalid"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--data", type=Path, default=root / "digits_crops")
    parser.add_argument("--quarantine", type=Path, default=root / "quarantined_crops")
    parser.add_argument("--report", type=Path, default=root / "second_bot" / "models" / "type1_cleaning_report.json")
    args = parser.parse_args()
    ocr = ddddocr.DdddOcr(show_ad=False)
    report: dict[str, object] = {"data": str(args.data), "quarantine": str(args.quarantine), "digits": {}}
    for label in _DIGITS:
        source = args.data / label
        target = args.quarantine / label
        files = sorted(p for p in source.glob("*.png") if p.is_file()) if source.is_dir() else []
        counts = {"before_count": len(files), "kept_count": 0, "quarantined_count": 0, "reasons": {}}
        for path in files:
            with Image.open(path) as image:
                keep, predicted, reason = classify(ocr, image, label)
            if keep:
                counts["kept_count"] += 1
            else:
                target.mkdir(parents=True, exist_ok=True)
                destination = target / path.name
                if destination.exists():
                    destination = target / f"{path.stem}__duplicate{path.suffix}"
                shutil.move(str(path), str(destination))
                counts["quarantined_count"] += 1
            reasons = counts["reasons"]
            assert isinstance(reasons, dict)
            key = f"{reason}:{predicted or '<empty>'}"
            reasons[key] = int(reasons.get(key, 0)) + 1
        report["digits"][label] = counts
        print(f"{label}: before_count={counts['before_count']} kept_count={counts['kept_count']} quarantined_count={counts['quarantined_count']}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
