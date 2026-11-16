"""Lightweight Type1 digit extraction using only NumPy and the stdlib.

The module deliberately does not depend on Pillow, OpenCV, SciPy, or any
other image package.  It reads uncompressed Windows BMP files, segments the
foreground with projection profiles, and classifies each glyph by Hamming
distance against 0-9 templates.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

import numpy as np


DEFAULT_THRESHOLD = 128

# Compact 5x7 templates.  A caller can pass higher-resolution templates to
# recognize_digit/recognize_bmp; these defaults make the module usable alone.
_DEFAULT_GLYPHS = (
    ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
)
DEFAULT_TEMPLATES = {
    str(digit): np.array([[char == "1" for char in row] for row in glyph], dtype=bool)
    for digit, glyph in enumerate(_DEFAULT_GLYPHS)
}


def _read_source(source: str | Path | bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "read"):
        return source.read()
    return Path(source).read_bytes()


def read_bmp(
    source: str | Path | bytes | bytearray | BinaryIO,
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[np.ndarray, np.ndarray]:
    """Read an uncompressed BMP and return ``(gray_uint8, binary_bool)``.

    Supported formats are 1-, 8-, 24-, and 32-bit BMP with a BI_RGB header.
    Binary foreground is represented by ``True`` and consists of pixels whose
    grayscale value is at most ``threshold`` (the usual dark digit case).
    """
    data = _read_source(source)
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP file")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    dib_size = struct.unpack_from("<I", data, 14)[0]
    if dib_size < 40 or len(data) < 14 + dib_size:
        raise ValueError("unsupported BMP header")
    width, signed_height, planes, bits, compression = struct.unpack_from(
        "<iiHHI", data, 18
    )
    if width <= 0 or signed_height == 0 or planes != 1:
        raise ValueError("invalid BMP dimensions")
    if bits not in (1, 8, 24, 32) or compression != 0:
        raise ValueError("only uncompressed 1/8/24/32-bit BMP is supported")
    height = abs(signed_height)
    top_down = signed_height < 0

    palette: np.ndarray | None = None
    if bits <= 8:
        colors_used = struct.unpack_from("<I", data, 46)[0]
        count = colors_used or (1 << bits)
        palette_start = 14 + dib_size
        palette_bytes = np.frombuffer(data, dtype=np.uint8, count=count * 4, offset=palette_start)
        if palette_bytes.size < count * 4:
            raise ValueError("truncated BMP palette")
        palette = palette_bytes.reshape(count, 4)[:, :3][:, ::-1].copy()

    row_stride = ((width * bits + 31) // 32) * 4
    end = pixel_offset + row_stride * height
    if pixel_offset < 14 + dib_size or end > len(data):
        raise ValueError("truncated BMP pixel data")
    raw = np.frombuffer(data, dtype=np.uint8, count=row_stride * height, offset=pixel_offset)
    raw = raw.reshape(height, row_stride)
    rows = raw if top_down else raw[::-1]

    if bits == 24:
        pixels = rows[:, : width * 3].reshape(height, width, 3)[:, :, ::-1]
        gray = np.rint(
            pixels[:, :, 0] * 0.299 + pixels[:, :, 1] * 0.587 + pixels[:, :, 2] * 0.114
        ).astype(np.uint8)
    elif bits == 32:
        pixels = rows[:, : width * 4].reshape(height, width, 4)[:, :, ::-1]
        gray = np.rint(
            pixels[:, :, 0] * 0.299 + pixels[:, :, 1] * 0.587 + pixels[:, :, 2] * 0.114
        ).astype(np.uint8)
    else:
        if bits == 8:
            indices = rows[:, :width]
        else:
            packed = rows[:, : (width + 7) // 8]
            indices = np.zeros((height, width), dtype=np.uint8)
            for bit in range(width):
                indices[:, bit] = (packed[:, bit // 8] >> (7 - bit % 8)) & 1
        assert palette is not None
        if np.any(indices >= len(palette)):
            raise ValueError("BMP palette index out of range")
        colors = palette[indices]
        gray = np.rint(
            colors[:, :, 0] * 0.299 + colors[:, :, 1] * 0.587 + colors[:, :, 2] * 0.114
        ).astype(np.uint8)
    if not 0 <= threshold <= 255:
        raise ValueError("threshold must be between 0 and 255")
    return gray, gray <= threshold


def binary_threshold(gray: np.ndarray, threshold: int = DEFAULT_THRESHOLD) -> np.ndarray:
    """Return a boolean dark-foreground mask from a grayscale array."""
    array = np.asarray(gray)
    if array.ndim != 2:
        raise ValueError("gray image must be a 2-D array")
    if not 0 <= threshold <= 255:
        raise ValueError("threshold must be between 0 and 255")
    return array <= threshold


def projection_profiles(binary: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return row and column foreground counts using NumPy reductions."""
    mask = np.asarray(binary, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("binary image must be a 2-D array")
    return mask.sum(axis=1, dtype=np.int32), mask.sum(axis=0, dtype=np.int32)


def _runs(values: np.ndarray, minimum: int) -> list[tuple[int, int]]:
    active = np.asarray(values) >= minimum
    padded = np.pad(active.astype(np.int8), (1, 1))
    changes = np.flatnonzero(np.diff(padded))
    return [(int(left), int(right)) for left, right in changes.reshape(-1, 2) if right > left]


def extract_digits(
    binary: np.ndarray,
    *,
    min_pixels: int = 2,
    min_width: int = 1,
    min_height: int = 1,
) -> list[np.ndarray]:
    """Crop foreground glyphs left-to-right using row/column projections.

    Empty columns and rows are ignored, and ``min_pixels`` suppresses small
    noise runs. Returned crops are boolean arrays in reading order.
    """
    mask = np.asarray(binary, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("binary image must be a 2-D array")
    if min_pixels < 1 or min_width < 1 or min_height < 1:
        raise ValueError("minimum sizes must be positive")
    row_projection, col_projection = projection_profiles(mask)
    rows = _runs(row_projection, 1)
    if not rows:
        return []
    top, bottom = rows[0][0], rows[-1][1]
    columns = _runs(col_projection, min_pixels)
    result: list[np.ndarray] = []
    for left, right in columns:
        crop = mask[top:bottom, left:right]
        if right - left >= min_width and crop.shape[0] >= min_height:
            result.append(crop.copy())
    return result


def extract_fixed_digits(
    binary: np.ndarray,
    *,
    count: int = 5,
    margin: float = 0.10,
) -> list[np.ndarray]:
    """Split a fixed-width Type1 image into equally spaced digit regions.

    Type1 images contain crossing lines that can join adjacent digits, making
    gap-based projection segmentation unreliable.  The labelled dataset uses
    a stable five-cell layout, so this fallback intentionally preserves each
    cell's geometry instead of looking for empty columns.
    """
    mask = np.asarray(binary, dtype=bool)
    if mask.ndim != 2 or count < 1 or not 0 <= margin < 0.5:
        raise ValueError("invalid binary image, count, or margin")
    width = mask.shape[1]
    left, right = round(width * margin), round(width * (1.0 - margin))
    return [mask[:, left + (right - left) * i // count : left + (right - left) * (i + 1) // count].copy()
            for i in range(count)]


def extract_shifted_digits(
    gray: np.ndarray,
    thresholds: Sequence[int],
    templates: Sequence[Mapping[str | int, Sequence[np.ndarray]]],
    *,
    margin: float = 0.10,
    local_shift: int = 6,
) -> list[np.ndarray]:
    """Choose locally shifted cell bounds by minimum template distance.

    Each nominal cell boundary is searched independently within ``local_shift``
    pixels.  The best crop is selected against the corresponding position's
    template bank, which corrects small glyph drift without running a costly
    global segmentation search.
    """
    image = np.asarray(gray)
    if image.ndim != 2 or len(thresholds) != 5 or len(templates) != 5:
        raise ValueError("gray image, thresholds, and templates must have five positions")
    if local_shift < 0:
        raise ValueError("local_shift must be non-negative")
    height, width = image.shape
    left, right = round(width * margin), round(width * (1.0 - margin))
    nominal = [left + (right - left) * i // 5 for i in range(6)]
    output: list[np.ndarray] = []
    for position in range(5):
        start0, end0 = nominal[position], nominal[position + 1]
        best: tuple[float, np.ndarray] | None = None
        # Translate the whole nominal cell together. This evaluates only
        # ``2 * local_shift + 1`` candidates instead of a quadratic boundary
        # grid, while correcting the dominant per-cell alignment drift.
        for delta in range(-local_shift, local_shift + 1):
            start, end = start0 + delta, end0 + delta
            if start < left or end > right or end - start < 2:
                continue
            crop = image[:, start:end] <= int(thresholds[position])
            distance = min(
                hamming_distance(normalize_glyph(crop), template)
                for values in templates[position].values()
                for template in values
            )
            if best is None or distance < best[0]:
                best = (distance, crop.copy())
        if best is None:
            raise ValueError(f"could not find a crop for position {position}")
        output.append(best[1])
    return output


def _trim_ink(mask: np.ndarray) -> np.ndarray:
    """Remove empty border rows/columns before comparing glyph geometry."""
    array = np.asarray(mask, dtype=bool)
    rows = np.flatnonzero(array.any(axis=1))
    columns = np.flatnonzero(array.any(axis=0))
    if rows.size == 0 or columns.size == 0:
        return array[:1, :1]
    return array[rows[0] : rows[-1] + 1, columns[0] : columns[-1] + 1]


def _resize_nearest(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    height, width = shape
    if height < 1 or width < 1:
        raise ValueError("template dimensions must be positive")
    source = np.asarray(mask, dtype=bool)
    y = np.minimum((np.arange(height) * source.shape[0]) // height, source.shape[0] - 1)
    x = np.minimum((np.arange(width) * source.shape[1]) // width, source.shape[1] - 1)
    return source[np.ix_(y, x)]


def hamming_distance(glyph: np.ndarray, template: np.ndarray) -> float:
    """Return normalized differing-pixel count after nearest-neighbor resize."""
    target = np.asarray(template, dtype=bool)
    source = np.asarray(glyph, dtype=bool)
    if target.ndim != 2 or source.ndim != 2:
        raise ValueError("glyph and template must be 2-D arrays")
    target = _trim_ink(target)
    resized = _resize_nearest(_trim_ink(source), target.shape)
    return float(np.count_nonzero(resized != target)) / target.size


def normalize_glyph(glyph: np.ndarray, shape: tuple[int, int] = (32, 24)) -> np.ndarray:
    """Trim empty borders and resize one glyph to a template shape."""
    return _resize_nearest(_trim_ink(np.asarray(glyph, dtype=bool)), shape)


def build_template_bank(
    glyphs: Sequence[Sequence[np.ndarray]],
    answers: Sequence[str],
    *,
    shape: tuple[int, int] = (32, 24),
    require_complete: bool = False,
) -> list[dict[str, list[np.ndarray]]]:
    """Build a position-aware bank of real normalized digit templates."""
    if len(glyphs) != len(answers):
        raise ValueError("glyph and answer counts must match")
    bank: list[dict[str, list[np.ndarray]]] = [
        {digit: [] for digit in "0123456789"} for _ in range(5)
    ]
    for image_glyphs, answer in zip(glyphs, answers):
        if len(image_glyphs) != 5 or len(answer) != 5 or not answer.isdigit():
            continue
        for position, (glyph, digit) in enumerate(zip(image_glyphs, answer)):
            bank[position][digit].append(normalize_glyph(glyph, shape))
    if require_complete and any(not bank[position][digit] for position in range(5) for digit in "0123456789"):
        raise ValueError("training data does not contain every digit at every position")
    return bank


def save_template_bank(bank: Sequence[Mapping[str, Sequence[np.ndarray]]], path: str | Path) -> Path:
    """Save a template bank as a compact, NumPy-only compressed archive."""
    if len(bank) != 5:
        raise ValueError("template bank must contain five positions")
    payload: dict[str, np.ndarray] = {}
    for position, templates in enumerate(bank):
        for digit in "0123456789":
            values = [np.asarray(item, dtype=bool) for item in templates.get(digit, ())]
            payload[f"p{position}d{digit}"] = (
                np.stack(values).astype(np.uint8) if values else np.empty((0,), dtype=np.uint8)
            )
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, **payload)
    return output


def load_template_bank(path: str | Path) -> list[dict[str, list[np.ndarray]]]:
    """Load a bank produced by :func:`save_template_bank`."""
    bank: list[dict[str, list[np.ndarray]]] = [{digit: [] for digit in "0123456789"} for _ in range(5)]
    with np.load(Path(path), allow_pickle=False) as archive:
        for position in range(5):
            for digit in "0123456789":
                key = f"p{position}d{digit}"
                if key not in archive:
                    raise ValueError(f"missing template archive entry: {key}")
                values = archive[key]
                bank[position][digit] = [item.astype(bool) for item in values] if values.ndim == 3 else []
    return bank


def match_digit(
    glyph: np.ndarray,
    templates: Mapping[str | int, np.ndarray] | None = None,
) -> tuple[str, float]:
    """Return ``(digit, normalized_hamming_distance)`` for one glyph."""
    table = DEFAULT_TEMPLATES if templates is None else templates
    if not table:
        raise ValueError("at least one digit template is required")
    ranked = [(hamming_distance(glyph, template), str(digit)) for digit, template in table.items()]
    distance, digit = min(ranked, key=lambda item: (item[0], item[1]))
    return digit, distance


def match_position(
    glyph: np.ndarray,
    templates: Mapping[str | int, Sequence[np.ndarray]],
) -> tuple[str, float]:
    """Match against all real templates belonging to one digit position."""
    ranked = [
        (min(hamming_distance(glyph, template) for template in values), str(digit))
        for digit, values in templates.items()
        if values
    ]
    if not ranked:
        raise ValueError("at least one position template is required")
    distance, digit = min(ranked, key=lambda item: (item[0], item[1]))
    return digit, distance


def recognize_bmp(
    source: str | Path | bytes | bytearray | BinaryIO,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    templates: Mapping[str | int, np.ndarray] | Sequence[Mapping[str | int, Sequence[np.ndarray]]] | None = None,
    min_pixels: int = 1,
    position_thresholds: Sequence[int] | None = None,
    local_shift: int = 0,
) -> tuple[str, list[float]]:
    """Read a BMP, segment its digits, and return text plus per-digit scores."""
    gray, binary = read_bmp(source, threshold=threshold)
    if templates is not None and isinstance(templates, Sequence) and not isinstance(templates, (str, bytes, bytearray, np.ndarray)):
        if position_thresholds is not None or local_shift:
            thresholds = tuple(position_thresholds or (threshold,) * 5)
            if len(thresholds) != 5:
                raise ValueError("position_thresholds must contain five values")
            glyphs = extract_shifted_digits(gray, thresholds, templates, local_shift=local_shift)
        else:
            glyphs = extract_fixed_digits(binary, count=len(templates))
        matches = [match_position(glyph, templates[index]) for index, glyph in enumerate(glyphs)]
    else:
        glyphs = extract_digits(binary, min_pixels=min_pixels)
        matches = [match_digit(glyph, templates) for glyph in glyphs]  # type: ignore[arg-type]
    return "".join(digit for digit, _ in matches), [distance for _, distance in matches]


# Explicit aliases keep the small module convenient for callers using either
# noun in their integration code.
read_bmp_raw = read_bmp
segment_digits = extract_digits
recognize = recognize_bmp

__all__ = [
    "DEFAULT_TEMPLATES",
    "binary_threshold",
    "build_template_bank",
    "extract_digits",
    "extract_fixed_digits",
    "extract_shifted_digits",
    "hamming_distance",
    "load_template_bank",
    "match_digit",
    "match_position",
    "normalize_glyph",
    "projection_profiles",
    "read_bmp",
    "read_bmp_raw",
    "recognize",
    "recognize_bmp",
    "save_template_bank",
    "segment_digits",
]
