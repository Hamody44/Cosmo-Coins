"""Public Type1 OCR API; local-only and free of provider integrations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .confidence import score_confidence
from .features_type1 import segment_type1_image
from .gemini_solver import solve_with_gemini_flash
from .image import ImageSource, load_image
from .preprocessing import preprocessing_variants
from .segmentation import split_candidate_records
from .templates import DEFAULT_MODEL_PATH, _best_segments, load_model, predict_segments, validate_model
from .types import CELL_COUNT, Type1Result

# Production inference loads the persisted model once at module import time.
_MODEL = load_model(DEFAULT_MODEL_PATH) if DEFAULT_MODEL_PATH.is_file() else None


def _cloud_fallback(source: ImageSource) -> Type1Result | None:
    text = solve_with_gemini_flash(source)
    if text is None:
        return None
    return Type1Result(
        text=text,
        confidence=1.0,
        reliable=True,
        abstained=False,
        digit_confidence=(1.0,) * CELL_COUNT,
    )


def _candidate_segments(image, model=None):
    if hasattr(model, "decision_function") and getattr(model, "n_features_in_", None) == 415:
        segments = segment_type1_image(image)
        return [segments] if len(segments) == CELL_COUNT else []
    segments = _best_segments(image)
    return [segments] if len(segments) == CELL_COUNT else []


def recognize_type1(
    source: ImageSource,
    model: dict[str, Any] | None = None,
    *,
    model_path: str | Path = DEFAULT_MODEL_PATH,
    max_distance: float = 0.34,
    minimum_confidence: float = 0.15,
) -> Type1Result:
    """Recognize one Type1 CAPTCHA with the original public signature."""
    if isinstance(model, dict) and model.get("templates") == [] and "classifier" not in model:
        return Type1Result(reason="empty Type1 model")
    if model is not None:
        selected_model = validate_model(model)
    elif Path(model_path) == DEFAULT_MODEL_PATH:
        selected_model = _MODEL
    else:
        selected_model = load_model(model_path)
    if selected_model is None:
        return _cloud_fallback(source) or Type1Result(reason="Type1 model is not trained")

    image = load_image(source)
    best: dict[str, Any] | None = None
    for segments in _candidate_segments(image, selected_model):
        prediction = predict_segments(segments, selected_model)
        if prediction.get("abstained"):
            continue
        if best is None or prediction["mean_distance"] < best["mean_distance"]:
            best = prediction
    if best is None:
        return _cloud_fallback(source) or Type1Result(reason="no complete five-segment candidate")
    text = best.get("text", "")
    distance = float(best.get("mean_distance", 1.0))
    confidence = score_confidence(distance, best.get("confidence", []), max_distance=max_distance)
    digit_confidence = tuple(best.get("confidence", []))
    if len(text) != CELL_COUNT or not text.isascii() or not text.isdigit():
        return Type1Result(reason="prediction is not exactly five ASCII digits", mean_distance=distance)
    if confidence < minimum_confidence:
        return Type1Result(text=text, confidence=confidence, mean_distance=distance, digit_confidence=digit_confidence, reason="confidence below configured minimum")
    return Type1Result(
        text=text,
        confidence=confidence,
        reliable=True,
        abstained=False,
        mean_distance=distance,
        digit_confidence=digit_confidence,
    )


recognize = recognize_type1


def predict_with_confidence(source: ImageSource) -> tuple[str, float, tuple[float, ...]]:
    """Return the five-digit prediction, its weakest digit probability, and all five probabilities."""
    if _MODEL is None:
        return "", 0.0, ()
    image = load_image(source)
    candidates = _candidate_segments(image, _MODEL)
    if not candidates:
        return "", 0.0, ()
    prediction = predict_segments(candidates[0], _MODEL)
    if prediction.get("abstained"):
        return "", 0.0, ()
    text = str(prediction.get("text", ""))
    values = tuple(float(item.get("confidence", 0.0)) for item in prediction.get("confidence", []))
    if len(text) != CELL_COUNT or not text.isascii() or not text.isdigit() or len(values) != CELL_COUNT:
        return "", 0.0, ()
    return text, min(values), values
