"""Safe inference helpers for the experimental type1 ONNX sequence OCR.

This module is intentionally separate from local_ocr.py so type2 behavior is
unchanged. It does not load a model at import time, and refuses to mark a
prediction reliable unless an externally tested model sidecar explicitly
qualifies it. The current bot does not call this module for type1 yet.
"""
from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import json
import math
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageOps

IMAGE_SIZE = (320, 112)  # width, height
CELL_COUNT = 5
BLANK_INDEX = 0
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "type1_sequence_ocr.onnx"


def load_image(source: bytes | str | Path | Image.Image) -> Image.Image:
    """Normalize a source image without stretching its aspect ratio."""
    if isinstance(source, Image.Image):
        image = source.copy()
    elif isinstance(source, bytes):
        image = Image.open(BytesIO(source))
    else:
        image = Image.open(source)
    image.load()
    image = ImageOps.exif_transpose(image).convert("L")
    image.thumbnail(IMAGE_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("L", IMAGE_SIZE, color=255)
    canvas.paste(image, ((IMAGE_SIZE[0] - image.width) // 2, (IMAGE_SIZE[1] - image.height) // 2))
    return canvas


def preprocess_image(source: bytes | str | Path | Image.Image):
    """Return one normalized NCHW float32 tensor for ONNX Runtime."""
    import numpy as np

    image = load_image(source)
    array = np.asarray(image, dtype=np.float32) / 127.5 - 1.0
    return array[None, None, :, :].astype(np.float32, copy=False)


def _softmax_rows(logits: Sequence[Sequence[float]]) -> list[list[float]]:
    rows: list[list[float]] = []
    for row in logits:
        if not row:
            continue
        maximum = max(float(value) for value in row)
        exp_values = [math.exp(float(value) - maximum) for value in row]
        total = sum(exp_values) or 1.0
        rows.append([value / total for value in exp_values])
    return rows


def ctc_greedy_decode(logits: Any, *, blank_index: int = BLANK_INDEX) -> dict[str, Any]:
    """Greedy CTC-decode [T,C] or singleton-batch [1,T,C] logits.

    The returned confidence is an uncalibrated mean posterior over emitted
    symbols; it must not be interpreted as a probability of a correct code.
    """
    if hasattr(logits, "tolist"):
        logits = logits.tolist()
    if not isinstance(logits, (list, tuple)) or not logits:
        return {"text": "", "mean_confidence": 0.0, "digit_confidences": []}
    if isinstance(logits[0], (list, tuple)) and logits[0] and isinstance(logits[0][0], (list, tuple)):
        if len(logits) != 1:
            raise ValueError("ctc_greedy_decode accepts one image at a time")
        logits = logits[0]
    probabilities = _softmax_rows(logits)
    digits: list[str] = []
    digit_confidences: list[float] = []
    previous = None
    for row in probabilities:
        index = max(range(len(row)), key=row.__getitem__)
        if index != previous and index != blank_index:
            if 1 <= index <= 10:
                digits.append(str(index - 1))
                digit_confidences.append(float(row[index]))
            else:
                # Unexpected classes are abstained rather than converted.
                return {"text": "", "mean_confidence": 0.0, "digit_confidences": []}
        previous = index
    mean_confidence = sum(digit_confidences) / len(digit_confidences) if digit_confidences else 0.0
    return {
        "text": "".join(digits),
        "mean_confidence": mean_confidence,
        "digit_confidences": digit_confidences,
    }


@lru_cache(maxsize=2)
def _open_session(model_path: str):
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError(
            "ONNX Runtime is not installed; install cosmo_type1_inference_requirements.txt."
        ) from exc
    return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])


def _read_sidecar(model_path: Path) -> dict[str, Any]:
    sidecar = model_path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        loaded = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def recognize(
    source: bytes | str | Path | Image.Image,
    *,
    model_path: str | Path = DEFAULT_MODEL_PATH,
) -> dict[str, Any]:
    """Run an optional ONNX model; remain conservative unless its sidecar qualifies it."""
    path = Path(model_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Type1 ONNX model is not installed: {path}")
    image = preprocess_image(source)
    session = _open_session(str(path))
    input_name = session.get_inputs()[0].name
    output = session.run(None, {input_name: image})[0]
    decoded = ctc_greedy_decode(output)
    metadata = _read_sidecar(path)
    threshold = float(metadata.get("confidence_threshold", 1.1))
    is_five_digits = len(decoded["text"]) == CELL_COUNT and decoded["text"].isascii() and decoded["text"].isdigit()
    reliable = bool(metadata.get("production_qualified")) and is_five_digits and decoded["mean_confidence"] >= threshold
    return {
        **decoded,
        "type": "type1",
        "model_name": metadata.get("model_name", "cosmo-type1-sequence-ctc"),
        "reliable": reliable,
        "qualification": metadata.get("qualification", "unqualified"),
    }
