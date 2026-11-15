"""Connected-component and projection-guided five-character segmentation."""
from __future__ import annotations

from itertools import combinations
from typing import Any

from PIL import Image

from .types import CELL_COUNT


def connected_components(mask: Image.Image, *, min_area: int = 3) -> list[tuple[int, int, int, int, int]]:
    """Find 4-connected foreground components without NumPy or OpenCV."""
    width, height = mask.size
    px = mask.load()
    seen = bytearray(width * height)
    output: list[tuple[int, int, int, int, int]] = []
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
                cx, cy = queue[head]
                head += 1
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


def ink_box(mask: Image.Image) -> tuple[int, int, int, int] | None:
    components = connected_components(mask, min_area=3)
    if not components:
        return None
    return (min(c[0] for c in components), min(c[1] for c in components),
            max(c[2] for c in components), max(c[3] for c in components))


def _column_ink(mask: Image.Image, left: int, right: int) -> list[int]:
    px = mask.load()
    return [sum(1 for y in range(mask.height) if px[x, y]) for x in range(left, right)]


def propose_splits(mask: Image.Image, *, max_candidates: int = 18) -> list[tuple[int, int, int, int, int, int]]:
    """Return bounded five-way cuts; non-uniform cuts support touching glyphs."""
    if max_candidates < 1:
        return []
    box = ink_box(mask)
    if not box:
        return []
    left, _, right, _ = box
    width = right - left
    if width < CELL_COUNT:
        return []
    ink = _column_ink(mask, left, right)
    gaps = sorted(range(1, width), key=lambda i: (ink[i - 1] + ink[i], abs(i - width / CELL_COUNT)))
    cuts = sorted(set([max(1, round(width * i / CELL_COUNT)) for i in range(1, CELL_COUNT)] + gaps[:18]))
    candidates: list[tuple[float, tuple[int, int, int, int, int]]] = []
    for a, b, c, d in combinations(cuts, 4):
        if not (0 < a < b < c < d < width):
            continue
        bounds = (left, left + a, left + b, left + c, left + d, right)
        widths = [bounds[i + 1] - bounds[i] for i in range(CELL_COUNT)]
        if min(widths) < max(2, width // 14) or max(widths) > max(40, width * 3 // 5):
            continue
        cut_cost = sum(ink[p - 1] + ink[p] for p in (a, b, c, d))
        balance = sum(abs(w - width / CELL_COUNT) for w in widths)
        candidates.append((cut_cost * 2 + balance, (a, b, c, d, width)))
    candidates.sort(key=lambda item: item[0])
    unique: list[tuple[int, int, int, int, int, int]] = []
    seen: set[tuple[int, int, int, int, int]] = set()
    for _, relative in candidates:
        if relative in seen:
            continue
        seen.add(relative)
        unique.append((left, left + relative[0], left + relative[1], left + relative[2], left + relative[3], right))
        if len(unique) >= max_candidates:
            break
    return unique


def _crop_segments(mask: Image.Image, bounds: tuple[int, int, int, int, int, int], top: int, bottom: int) -> list[Image.Image]:
    return [mask.crop((bounds[i], top, bounds[i + 1], bottom)) for i in range(CELL_COUNT)]


def split_candidate_records(mask: Image.Image, *, max_candidates: int = 18) -> list[dict[str, Any]]:
    box = ink_box(mask)
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


def segment_candidates(mask: Image.Image, *, max_candidates: int = 18) -> list[list[Image.Image]]:
    return [record["segments"] for record in split_candidate_records(mask, max_candidates=max_candidates)]
