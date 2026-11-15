"""Conservative local OCR for five-digit type1 images.

Only Pillow and the Python standard library are used.  The recognizer first
creates several threshold masks, proposes non-uniform five-way cuts, and then
scores each segment against position-aware digit templates.  It is deliberately
separate from ``local_ocr.py`` so type2 behavior remains unchanged.
"""
from __future__ import annotations

from io import BytesIO
import json
import math
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

CELL_COUNT = 5
IMAGE_SIZE = (300, 110)
FEATURE_SIZE = (24, 40)
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "type1_local_ocr.json"


def load_image(source: bytes | str | Path | Image.Image) -> Image.Image:
    if isinstance(source, Image.Image):
        image = source.copy()
    elif isinstance(source, bytes):
        image = Image.open(BytesIO(source))
    else:
        image = Image.open(source)
    image.load()
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.size != IMAGE_SIZE:
        image = image.resize(IMAGE_SIZE, Image.Resampling.LANCZOS)
    return image


def _gray(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(load_image(image))
    # Mild contrast stretch, without cropping or morphological deletion.
    return ImageEnhance.Contrast(gray).enhance(1.35)


def preprocessing_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """Return conservative dark-ink masks; background subtraction is optional."""
    gray = _gray(image)
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=7))
    variants: list[tuple[str, Image.Image]] = []
    for name, threshold in (("global_105", 105), ("global_135", 135), ("global_165", 165)):
        variants.append((name, gray.point(lambda p, t=threshold: 255 if p < t else 0)))
    # A local-background mask keeps dark strokes when the background is uneven.
    local = Image.eval(Image.new("L", gray.size, 0), lambda _: 0)
    gp, bp, lp = gray.load(), blurred.load(), local.load()
    for y in range(gray.height):
        for x in range(gray.width):
            lp[x, y] = 255 if gp[x, y] < bp[x, y] - 18 else 0
    variants.append(("background_delta", local))
    return variants


def _components(mask: Image.Image, *, min_area: int = 3) -> list[tuple[int, int, int, int, int]]:
    """Connected components using PixelAccess and standard Python queues."""
    width, height = mask.size
    px = mask.load()
    seen = bytearray(width * height)
    output = []
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if seen[index] or not px[x, y]:
                continue
            seen[index] = 1
            queue = [(x, y)]
            head = 0
            left = right = x
            top = bottom = y
            area = 0
            while head < len(queue):
                cx, cy = queue[head]; head += 1
                area += 1
                left, right = min(left, cx), max(right, cx)
                top, bottom = min(top, cy), max(bottom, cy)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < width and 0 <= ny < height:
                        ni = ny * width + nx
                        if not seen[ni] and px[nx, ny]:
                            seen[ni] = 1
                            queue.append((nx, ny))
            if area >= min_area:
                output.append((left, top, right + 1, bottom + 1, area))
    return output


def _ink_box(mask: Image.Image) -> tuple[int, int, int, int] | None:
    comps = _components(mask, min_area=3)
    if not comps:
        return None
    useful = [c for c in comps if c[4] >= 3]
    return (min(c[0] for c in useful), min(c[1] for c in useful),
            max(c[2] for c in useful), max(c[3] for c in useful))


def _column_ink(mask: Image.Image, left: int, right: int) -> list[int]:
    px = mask.load()
    return [sum(1 for y in range(mask.height) if px[x, y]) for x in range(left, right)]


def propose_splits(mask: Image.Image, *, max_candidates: int = 18) -> list[tuple[int, int, int, int, int, int]]:
    """Return a bounded set of five-way, non-equal cut proposals."""
    box = _ink_box(mask)
    if not box:
        return []
    left, _, right, _ = box
    width = right - left
    if width < CELL_COUNT:
        return []
    ink = _column_ink(mask, left, right)
    gaps = sorted(range(1, width), key=lambda i: (ink[i - 1] + ink[i], abs(i - width / 5)))
    # Include projection valleys and a small regular scaffold; never enumerate all paths.
    cuts = sorted(set([max(1, round(width * i / CELL_COUNT)) for i in range(1, CELL_COUNT)] + gaps[:18]))
    candidates: list[tuple[float, tuple[int, int, int, int, int]]] = []
    for a in cuts:
        for b in cuts:
            for c in cuts:
                for d in cuts:
                    if not (0 < a < b < c < d < width):
                        continue
                    bounds = (left, left + a, left + b, left + c, left + d, right)
                    widths = [bounds[i + 1] - bounds[i] for i in range(5)]
                    if min(widths) < max(2, width // 14) or max(widths) > max(40, width * 3 // 5):
                        continue
                    cut_cost = sum(ink[p - 1] + ink[p] for p in (a, b, c, d))
                    balance = sum(abs(w - width / 5) for w in widths)
                    candidates.append((cut_cost * 2 + balance, (a, b, c, d, width)))
    candidates.sort(key=lambda item: item[0])
    unique = []
    seen = set()
    for _, rel in candidates:
        if rel in seen:
            continue
        seen.add(rel)
        unique.append((left, left + rel[0], left + rel[1], left + rel[2], left + rel[3], right))
        if len(unique) >= max_candidates:
            break
    return unique


def split_candidate_records(mask: Image.Image, *, max_candidates: int = 18) -> list[dict[str, Any]]:
    """Return real x-boundaries plus ink-aware and geometry costs.

    Keeping the metadata prevents later fusion code from accidentally comparing
    only segment widths.  The cut cost is measured at the actual four boundary
    columns, while geometry cost is retained as a weak tie breaker.
    """
    box = _ink_box(mask)
    if not box:
        return []
    left, _, right, _ = box
    width = right - left
    ink = _column_ink(mask, left, right)
    records = []
    for bounds in propose_splits(mask, max_candidates=max_candidates):
        relative = [value - left for value in bounds]
        widths = [bounds[i + 1] - bounds[i] for i in range(CELL_COUNT)]
        cut_cost = sum(ink[p - 1] + ink[p] for p in relative[1:-1]) / max(1, mask.height * 2 * 4)
        geometry_cost = sum(abs(item - width / CELL_COUNT) for item in widths) / max(1, width)
        records.append({
            "bounds": bounds,
            "cut_cost": cut_cost,
            "geometry_cost": geometry_cost,
            "segments": _crop_segments(mask, bounds, max(0, box[1] - 2), min(mask.height, box[3] + 2)),
        })
    records.sort(key=lambda item: (item["cut_cost"] + 0.35 * item["geometry_cost"], item["geometry_cost"]))
    return records[:max_candidates]


def _crop_segments(mask: Image.Image, bounds: tuple[int, int, int, int, int, int], top: int, bottom: int) -> list[Image.Image]:
    return [mask.crop((bounds[i], top, bounds[i + 1], bottom)) for i in range(5)]


def segment_candidates(mask: Image.Image, *, max_candidates: int = 18) -> list[list[Image.Image]]:
    return [record["segments"] for record in split_candidate_records(mask, max_candidates=max_candidates)]


def _feature(segment: Image.Image) -> list[float]:
    box = segment.getbbox()
    canvas = Image.new("L", FEATURE_SIZE, 0)
    if box:
        crop = segment.crop(box)
        scale = min((FEATURE_SIZE[0] - 4) / crop.width, (FEATURE_SIZE[1] - 4) / crop.height)
        size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        crop = crop.resize(size, Image.Resampling.BILINEAR)
        canvas.paste(crop, ((FEATURE_SIZE[0] - size[0]) // 2, (FEATURE_SIZE[1] - size[1]) // 2))
    values = [p / 255.0 for p in canvas.tobytes()]
    # Add horizontal and vertical projections for interpretability and robustness.
    px = canvas.load()
    for y in range(FEATURE_SIZE[1]):
        values.append(sum(px[x, y] for x in range(FEATURE_SIZE[0])) / (255 * FEATURE_SIZE[0]))
    for x in range(FEATURE_SIZE[0]):
        values.append(sum(px[x, y] for y in range(FEATURE_SIZE[1])) / (255 * FEATURE_SIZE[1]))
    return values


def _distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / max(1, len(a)))


def extract_training_features(image: Image.Image, answer: str) -> list[list[float]]:
    """Use the best bounded split for a labelled image, preserving known order."""
    masks = preprocessing_variants(image)
    best: tuple[float, list[Image.Image]] | None = None
    for _, mask in masks:
        for segments in segment_candidates(mask, max_candidates=18):
            widths = [s.width for s in segments]
            score = sum(abs(w - sum(widths) / 5) for w in widths)
            if best is None or score < best[0]:
                best = (score, segments)
    if best is None:
        return []
    return [_feature(segment) for segment in best[1]]


def build_model(rows: Iterable[dict[str, str]], data_dir: Path) -> dict[str, Any]:
    rows = list(rows)
    # Pass 1: geometry-only templates provide a provisional reference.
    provisional = []
    prepared = []
    for row in rows:
        features = extract_training_features(Image.open(data_dir / row["filename"]), row["answer"])
        if len(features) != CELL_COUNT:
            continue
        prepared.append((row, features))
        for position, (digit, feature) in enumerate(zip(row["answer"], features)):
            provisional.append({"position": position, "digit": digit, "feature": feature})
    # Pass 2: select each labelled training split by the correct-position
    # template cost plus actual ink-cut cost. Validation/test labels never enter.
    selected_templates = []
    for row, _ in prepared:
        best: tuple[float, list[Image.Image]] | None = None
        for _, mask in preprocessing_variants(Image.open(data_dir / row["filename"])):
            for record in split_candidate_records(mask, max_candidates=18):
                features = [_feature(segment) for segment in record["segments"]]
                if len(features) != CELL_COUNT:
                    continue
                template_cost = sum(
                    min((_distance(features[pos], item["feature"])
                         for item in provisional
                         if item["position"] == pos and item["digit"] == row["answer"][pos]),
                        default=1.0)
                    for pos in range(CELL_COUNT)
                ) / CELL_COUNT
                score = template_cost + 0.20 * record["cut_cost"] + 0.10 * record["geometry_cost"]
                if best is None or score < best[0]:
                    best = (score, record["segments"])
        if best is not None:
            for position, (digit, feature) in enumerate(zip(row["answer"], map(_feature, best[1]))):
                selected_templates.append({"position": position, "digit": digit, "feature": feature})
    return {"schema_version": 3, "feature_size": list(FEATURE_SIZE), "training_sources": len(selected_templates) // CELL_COUNT, "templates": selected_templates}


def _predict_segments(segments: list[Image.Image], model: dict[str, Any]) -> dict[str, Any]:
    templates = model.get("templates", [])
    text, confidences = "", []
    for position, segment in enumerate(segments):
        feature = _feature(segment)
        ranked = []
        for digit in "0123456789":
            candidates = [t for t in templates if t.get("position") == position and str(t.get("digit")) == digit]
            if candidates:
                ranked.append((min(_distance(feature, t["feature"]) for t in candidates), digit))
        ranked.sort()
        if not ranked:
            return {"text": "", "confidence": [], "abstained": True}
        best = ranked[0][0]; second = ranked[1][0] if len(ranked) > 1 else best + 1
        margin = max(0.0, second - best)
        text += ranked[0][1]
        confidences.append({"digit": ranked[0][1], "distance": best, "margin": margin})
    mean_distance = sum(c["distance"] for c in confidences) / 5
    return {"text": text, "confidence": confidences, "mean_distance": mean_distance, "abstained": False}


def recognize(source: bytes | str | Path | Image.Image, model: dict[str, Any] | None = None, *, model_path: str | Path = DEFAULT_MODEL_PATH, max_distance: float = 0.34) -> dict[str, Any]:
    if model is None:
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Type1 local OCR model is not installed: {path}")
        model = json.loads(path.read_text(encoding="utf-8"))
    best: dict[str, Any] | None = None
    for _, mask in preprocessing_variants(load_image(source)):
        for segments in segment_candidates(mask):
            result = _predict_segments(segments, model)
            if result.get("abstained"):
                continue
            if best is None or result["mean_distance"] < best["mean_distance"]:
                best = result
    if best is None or len(best.get("text", "")) != CELL_COUNT or best["mean_distance"] > max_distance:
        return {"text": "", "type": "type1", "confidence": [], "abstained": True, "reliable": False}
    best.update({"type": "type1", "reliable": True})
    return best
