"""Reusable, local-only Type1 OCR library built with Python and Pillow."""
from .api import recognize, recognize_type1
from .confidence import is_confident, safe_consensus, score_confidence
from .errors import ImageDecodeError, ModelFormatError, Type1OCRError
from .features import extract_features
from .image import load_image
from .templates import build_model, load_model, save_model
from .types import CELL_COUNT, FEATURE_SIZE, IMAGE_SIZE, Type1Result

__all__ = [
    "CELL_COUNT", "FEATURE_SIZE", "IMAGE_SIZE", "ImageDecodeError", "ModelFormatError",
    "Type1OCRError", "Type1Result", "build_model", "is_confident", "load_image",
    "load_model", "recognize", "recognize_type1", "safe_consensus", "save_model",
    "score_confidence", "extract_features",
]
