"""Public Type1 OCR API; local-only and free of provider/fallback integrations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .confidence import score_confidence
from .image import ImageSource, load_image
from .preprocessing import preprocessing_variants
from .segmentation import segment_candidates
from .templates import DEFAULT_MODEL_PATH, load_model, predict_segments, validate_model
from .types import CELL_COUNT, Type1Result


def recognize_type1(
    source: ImageSource,
    model: dict[str, Any] | None = None,
    *,
    model_path: str | Path = DEFAULT_MODEL_PATH,
    max_distance: float = 0.34,
    minimum_confidence: float = 0.15,
) -> Type1Result:
    """Recognize one Type1 CAPTCHA; abstain when evidence is weak or malformed.

    Network providers and fallback integrations are intentionally outside this
    package. Applications should use their existing fallback when abstained.
    """
    selected_model = validate_model(model) if model is not None else load_model(model_path)
    image = load_image(source)
    best: dict[str, Any] | None = None
    for _, mask in preprocessing_variants(image):
        for segments in segment_candidates(mask):
            prediction = predict_segments(segments, selected_model)
            if prediction.get("abstained"):
                continue
            if best is None or prediction["mean_distance"] < best["mean_distance"]:
                best = prediction
    if best is None:
        return Type1Result(reason="no complete five-segment candidate")
    text = best.get("text", "")
    distance = float(best.get("mean_distance", float("inf")))
    confidence = score_confidence(distance, best.get("confidence", []), max_distance=max_distance)
    if len(text) != CELL_COUNT or not text.isascii() or not text.isdigit():
        return Type1Result(reason="prediction is not exactly five ASCII digits", mean_distance=distance)
    if distance > max_distance:
        return Type1Result(reason="mean template distance exceeds configured limit", mean_distance=distance, confidence=confidence)
    if confidence < minimum_confidence:
        return Type1Result(reason="confidence below configured minimum", mean_distance=distance, confidence=confidence)
    return Type1Result(
        text=text,
        confidence=confidence,
        reliable=True,
        abstained=False,
        mean_distance=distance,
        digit_confidence=tuple(best.get("confidence", [])),
    )


# Short public alias for consumers that scope imports to this package.
recognize = recognize_type1
