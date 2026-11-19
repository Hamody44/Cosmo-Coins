#!/usr/bin/env python3
"""Crop labeled five-digit CAPTCHA images from every fetched Git ref.

Image processing is implemented with Pillow and NumPy only. Ground-truth
sequences are read from repository JSON benchmark/model metadata because the
image filenames in this repository are sample IDs rather than the labels.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "second_bot"))
from type1_ocr.features_type1 import segment_type1_image

OUT = REPO / "digits_crops"
EXPECTED_SIZE = (300, 110)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
LABEL_FIELDS = ("expected", "answer", "ground_truth", "truth", "label")
DIVIDER_RATIOS = (0.25, 0.41, 0.57, 0.74)


def git(*args: str, binary: bool = False):
    result = subprocess.run(
        ["git", *args], cwd=REPO, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def natural_key(value: str) -> str:
    return re.sub(r"\d+", lambda m: m.group(0).zfill(12), value.casefold())


def refs_to_scan() -> list[str]:
    refs = git("for-each-ref", "--format=%(refname)", "refs/remotes/origin", "refs/heads").splitlines()
    refs = [r for r in refs if not r.endswith("/HEAD")]
    refs.sort(key=lambda ref: (not ref.startswith("refs/remotes/origin/"), ref.casefold()))
    # Avoid processing the same commit multiple times through local and remote refs.
    out, seen = [], set()
    for ref in refs:
        commit = git("rev-parse", ref).strip()
        if commit not in seen:
            seen.add(commit)
            out.append(ref)
    return out


def walk_label_records(value, by_filename):
    if isinstance(value, dict):
        filename = value.get("filename")
        if isinstance(filename, str):
            basename = Path(filename).name
            for key in LABEL_FIELDS:
                label = value.get(key)
                if isinstance(label, (str, int)):
                    text = str(label)
                    if re.fullmatch(r"\d{5}", text):
                        by_filename[basename].add(text)
        for child in value.values():
            walk_label_records(child, by_filename)
    elif isinstance(value, list):
        for child in value:
            walk_label_records(child, by_filename)


def collect_sources():
    """Find unique labeled 300x110 images in every available branch/tree."""
    sources = []
    seen_blobs = set()
    branches_scanned = []

    for ref in refs_to_scan():
        branches_scanned.append(ref)
        tree = git("ls-tree", "-r", "-z", ref, binary=True)
        entries = []
        for raw_entry in tree.split(b"\0"):
            if not raw_entry:
                continue
            metadata, raw_path = raw_entry.split(b"\t", 1)
            _mode, kind, oid = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8", "replace")
            entries.append((kind, oid, path))

        labels = defaultdict(set)
        for kind, oid, path in entries:
            if kind != "blob" or Path(path).suffix.casefold() != ".json":
                continue
            try:
                doc = json.loads(git("cat-file", "blob", oid, binary=True))
            except (json.JSONDecodeError, UnicodeDecodeError, subprocess.CalledProcessError):
                continue
            walk_label_records(doc, labels)

        for kind, oid, path in entries:
            suffix = Path(path).suffix.casefold()
            if kind != "blob" or suffix not in IMAGE_EXTENSIONS:
                continue
            name = Path(path).name
            truths = labels.get(name, set())
            if len(truths) != 1:
                continue
            label = next(iter(truths))
            blob_key = (oid, label)
            if blob_key in seen_blobs:
                continue
            data = git("cat-file", "blob", oid, binary=True)
            try:
                with Image.open(io.BytesIO(data)) as im:
                    size = im.size
            except Exception:
                continue
            if size != EXPECTED_SIZE:
                continue
            seen_blobs.add(blob_key)
            sources.append({
                "ref": ref,
                "path": path,
                "filename": name,
                "oid": oid,
                "label": label,
                "data": data,
            })

    sources.sort(key=lambda item: (item["ref"].casefold(), natural_key(item["path"])))
    return sources, branches_scanned


def box_sum_7x7(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), ((3, 3), (3, 3)), constant_values=0)
    integral = np.pad(padded, ((1, 0), (1, 0)), constant_values=0).cumsum(0).cumsum(1)
    return (
        integral[7:, 7:] - integral[:-7, 7:]
        - integral[7:, :-7] + integral[:-7, :-7]
    )


def remove_long_lines(mask: np.ndarray) -> np.ndarray:
    """Erase detected >=80px straight ink runs only at low local ink density."""
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if len(xs) < 80:
        return mask.copy()

    xf, yf = xs.astype(np.float64), ys.astype(np.float64)
    candidates = np.zeros(len(xs), dtype=bool)

    for angle in range(0, 176, 5):
        theta = math.radians(angle)
        ct, st = math.cos(theta), math.sin(theta)
        rho_float = xf * ct + yf * st
        rho_bins = np.rint(rho_float).astype(np.int32)
        t = -xf * st + yf * ct
        low = int(rho_bins.min())
        counts = np.bincount(rho_bins - low)

        for bin_index in np.flatnonzero(counts >= 80):
            indices = np.flatnonzero(rho_bins == bin_index + low)
            if len(indices) < 80:
                continue
            order = np.argsort(t[indices])
            sorted_indices = indices[order]
            sorted_t = t[sorted_indices]
            # Small rasterization gaps are bridged, but separated glyphs are not.
            breaks = np.flatnonzero(np.diff(sorted_t) > 3.0) + 1
            for group in np.split(sorted_indices, breaks):
                if len(group) < 80 or t[group].max() - t[group].min() < 79.0:
                    continue
                rho_center = float(np.median(rho_float[group]))
                t0, t1 = float(t[group].min()) - 2.0, float(t[group].max()) + 2.0
                near = (np.abs(rho_float - rho_center) <= 1.25) & (t >= t0) & (t <= t1)
                candidates |= near

    density = box_sum_7x7(mask) / 49.0
    result = mask.copy()
    remove = candidates & (density[ys, xs] < 0.72)
    result[ys[remove], xs[remove]] = False
    return result


def find_dividers(mask: np.ndarray) -> list[int]:
    h, w = mask.shape
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    projection = mask[y0:y1].sum(axis=0)
    radius = int(round(0.06 * w))
    dividers = []
    for ratio in DIVIDER_RATIOS:
        expected = ratio * w
        lo = max(1, int(math.floor(expected - radius)))
        hi = min(w - 1, int(math.ceil(expected + radius)) + 1)
        positions = np.arange(lo, hi)
        values = projection[lo:hi]
        minimum = values.min()
        tied = positions[values == minimum]
        # A flat minimum is resolved toward the expected divider.
        dividers.append(int(min(tied, key=lambda x: abs(x - expected))))
    return dividers


def tight_digit_crop(mask: np.ndarray, x0: int, x1: int) -> np.ndarray:
    """Tight crop using central-band support so stray outer circles don't set bounds."""
    h, w = mask.shape
    y0, y1 = int(round(0.22 * h)), int(round(0.78 * h))
    region = mask[:, x0:x1]
    central = region[y0:y1]
    col_count = central.sum(axis=0)
    global_x = np.arange(x0, x1)

    # Ignore image-edge specks/circles and isolated low-support columns.
    support_floor = max(2, int(round(0.06 * (y1 - y0))))
    valid = (col_count >= support_floor) & (global_x >= int(0.04 * w)) & (global_x < int(0.96 * w))
    supported = np.flatnonzero(valid)
    if len(supported) == 0:
        supported = np.flatnonzero(col_count > 0)
    if len(supported) == 0:
        supported = np.flatnonzero(region.any(axis=0))
    if len(supported) == 0:
        # Keep an empty, small white crop rather than failing the full dataset.
        return np.full((1, 1), 255, dtype=np.uint8)

    # Choose the strongest central-band projection run; stray edge circles and
    # isolated crossing-line fragments form weaker, separate runs.
    breaks = np.flatnonzero(np.diff(supported) > 3) + 1
    runs = np.split(supported, breaks)
    active = max(runs, key=lambda run: int(col_count[run].sum()))
    left, right = x0 + int(active[0]), x0 + int(active[-1]) + 1
    # Base tight bounds on the largest connected glyph component. Stray
    # detached circles/noise outside this component must not stretch the crop.
    core = mask[:, left:right]
    seen = np.zeros(core.shape, dtype=bool)
    best_area, best_box = 0, None
    foreground = np.argwhere(core)
    for start_y, start_x in foreground:
        if seen[start_y, start_x]:
            continue
        seen[start_y, start_x] = True
        stack = [(int(start_y), int(start_x))]
        min_y = max_y = int(start_y)
        min_x = max_x = int(start_x)
        area = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            min_y, max_y = min(min_y, cy), max(max_y, cy)
            min_x, max_x = min(min_x, cx), max(max_x, cx)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if (dy or dx) and 0 <= ny < core.shape[0] and 0 <= nx < core.shape[1]:
                        if core[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
        if area > best_area:
            best_area = area
            best_box = (min_y, max_y + 1, min_x, max_x + 1)

    if best_box is None:
        return np.full((1, 1), 255, dtype=np.uint8)
    top, bottom, rel_left, rel_right = best_box
    left = max(x0, left + rel_left - 2)
    right = min(x1, left + (rel_right - rel_left) + 4)
    top = max(0, top - 2)
    bottom = min(h, bottom + 2)
    digit = mask[top:bottom, left:right]
    return np.where(digit, 0, 255).astype(np.uint8)


def process_source(source):
    with Image.open(io.BytesIO(source["data"])) as im:
        return [np.asarray(crop, dtype=np.uint8) for crop in segment_type1_image(im)]


def load_font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_preview(crops_by_digit: dict[str, list[Path]], output: Path):
    cols, rows = 5, 4
    cell_w, cell_h = 150, 116
    header_h, section_gap = 34, 12
    margin = 12
    section_h = header_h + rows * cell_h + 8
    width = margin * 2 + cols * cell_w
    height = margin + 10 * (section_h + section_gap)
    canvas = Image.new("RGB", (width, height), (235, 238, 242))
    draw = ImageDraw.Draw(canvas)
    title_font, caption_font = load_font(18), load_font(10)
    thumb_w, thumb_h = 124, 82

    for digit in range(10):
        name = str(digit)
        paths = sorted(crops_by_digit[name], key=lambda p: p.name)[:20]
        section_y = margin + digit * (section_h + section_gap)
        draw.rounded_rectangle(
            (margin, section_y, width - margin, section_y + section_h),
            radius=9, fill=(255, 255, 255), outline=(205, 210, 218), width=1,
        )
        draw.text((margin + 10, section_y + 7), f"Digit {digit} — {len(paths)} samples", fill=(27, 38, 54), font=title_font)

        for i, path in enumerate(paths):
            row, col = divmod(i, cols)
            cell_x = margin + col * cell_w
            cell_y = section_y + header_h + row * cell_h
            draw.rounded_rectangle(
                (cell_x + 5, cell_y + 3, cell_x + cell_w - 5, cell_y + cell_h - 4),
                radius=5, fill=(248, 249, 251), outline=(226, 229, 233), width=1,
            )
            with Image.open(path) as im:
                thumb = im.convert("L")
                thumb.thumbnail((thumb_w, thumb_h), Image.Resampling.NEAREST)
                preview = Image.new("RGB", thumb.size, "white")
                preview.paste(thumb.convert("RGB"))
            px = cell_x + (cell_w - preview.width) // 2
            py = cell_y + 6 + max(0, (thumb_h - preview.height) // 2)
            canvas.paste(preview, (px, py))
            caption = path.name
            draw.text((cell_x + 9, cell_y + 91), caption, fill=(56, 63, 72), font=caption_font)

    canvas.save(output, format="PNG", optimize=True)


def main():
    sources, refs = collect_sources()
    if not sources:
        raise SystemExit("No labeled 300x110 images were found in the available Git refs.")

    OUT.mkdir(parents=True, exist_ok=True)
    for digit in range(10):
        folder = OUT / str(digit)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.png"):
            old.unlink()
    preview = OUT / "preview_grid.png"
    if preview.exists():
        preview.unlink()

    manifest_path = OUT / "manifest.csv"
    crops_by_digit = {str(i): [] for i in range(10)}
    saved_counts = {str(i): 0 for i in range(10)}
    with manifest_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(("source_ref", "source_path", "ground_truth", "position_1_based", "digit", "crop_file", "blob_oid"))
        for index, source in enumerate(sources, start=1):
            crops = process_source(source)
            stem = f"img{index:03d}"
            for pos, crop in enumerate(crops, start=1):
                digit = source["label"][pos - 1]
                filename = f"{stem}_pos{pos}.png"
                destination = OUT / digit / filename
                Image.fromarray(crop, mode="L").save(destination, format="PNG", optimize=True)
                crops_by_digit[digit].append(destination)
                saved_counts[digit] += 1
                writer.writerow((source["ref"], source["path"], source["label"], pos, digit, filename, source["oid"]))

    make_preview(crops_by_digit, preview)
    print(f"Git refs scanned: {', '.join(refs)}")
    print(f"Valid labeled captcha images processed: {len(sources)} (300x110)")
    print(f"Individual digit crops saved: {sum(saved_counts.values())}")
    print("Saved crops by digit folder:")
    for digit in range(10):
        print(f"  {digit}: {saved_counts[str(digit)]}")
    print(f"Preview grid: {preview}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
