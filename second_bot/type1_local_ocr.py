"""Compatibility facade for the reusable :mod:`type1_ocr` package.

Existing bot code and scripts may continue importing this module. New code
should import from ``type1_ocr`` directly. OCR.Space/Gemini remain in main.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from PIL import Image

try:  # package import, e.g. second_bot.type1_local_ocr
    from .type1_ocr.api import recognize_type1
    from .type1_ocr.features import extract_feature as _feature, feature_distance as _distance
    from .type1_ocr.image import load_image
    from .type1_ocr.preprocessing import grayscale as _gray, preprocessing_variants
    from .type1_ocr.segmentation import (
        connected_components as _components,
        ink_box as _ink_box,
        propose_splits,
        segment_candidates,
        split_candidate_records,
    )
    from .type1_ocr.templates import (
        DEFAULT_MODEL_PATH,
        build_model,
        extract_training_features,
        predict_segments as _predict_segments,
    )
    from .type1_ocr.types import CELL_COUNT, FEATURE_SIZE, IMAGE_SIZE
except ImportError:  # direct import from the bot's working directory
    from type1_ocr.api import recognize_type1
    from type1_ocr.features import extract_feature as _feature, feature_distance as _distance
    from type1_ocr.image import load_image
    from type1_ocr.preprocessing import grayscale as _gray, preprocessing_variants
    from type1_ocr.segmentation import (
        connected_components as _components,
        ink_box as _ink_box,
        propose_splits,
        segment_candidates,
        split_candidate_records,
    )
    from type1_ocr.templates import (
        DEFAULT_MODEL_PATH,
        build_model,
        extract_training_features,
        predict_segments as _predict_segments,
    )
    from type1_ocr.types import CELL_COUNT, FEATURE_SIZE, IMAGE_SIZE


def recognize(source: Any, model: dict[str, Any] | None = None, *, model_path: str | Path = DEFAULT_MODEL_PATH, max_distance: float = 0.34) -> dict[str, Any]:
    """Legacy dict-shaped API; delegates all OCR work to the package."""
    result = recognize_type1(
        source, model, model_path=model_path, max_distance=max_distance, minimum_confidence=0.0
    )
    if result.abstained:
        return {"text": "", "type": "type1", "confidence": [], "abstained": True, "reliable": False}
    return {
        "text": result.text,
        "type": "type1",
        "confidence": list(result.digit_confidence),
        "mean_distance": result.mean_distance,
        "abstained": False,
        "reliable": result.reliable,
    }
