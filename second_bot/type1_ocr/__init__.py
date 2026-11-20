"""Reusable local Type1 OCR library built with Pillow and NumPy."""
import importlib
import sys
import numpy as np

# Register the import alias before api.py loads package-local OCR modules when
# this package is imported as ``second_bot.type1_ocr`` from the repository root.
if __name__ != "type1_ocr":
    _package_alias = sys.modules.setdefault("type1_ocr", sys.modules[__name__])
    if _package_alias is sys.modules[__name__]:
        _templates_module = importlib.import_module(".templates", __name__)
        sys.modules.setdefault("type1_ocr.templates", _templates_module)

from .api import predict_with_confidence, recognize, recognize_type1
from .confidence import is_confident, safe_consensus, score_confidence
from .errors import ImageDecodeError, ModelFormatError, Type1OCRError
try:
    from .features import extract_features
except ImportError:  # compatibility with older deployments exposing only extract_feature
    from .features import extract_feature as _extract_feature

    def extract_features(crop):
        return np.asarray(_extract_feature(crop), dtype=np.float32)

from .features_type1 import FEATURE_LENGTH as TYPE1_FEATURE_LENGTH, extract_features as extract_features_type1
from .image import load_image
from .templates import build_model, load_model, save_model
from .types import CELL_COUNT, FEATURE_SIZE, IMAGE_SIZE, Type1Result

__all__ = [
    "CELL_COUNT", "FEATURE_SIZE", "IMAGE_SIZE", "ImageDecodeError", "ModelFormatError",
    "Type1OCRError", "Type1Result", "build_model", "is_confident", "load_image",
    "load_model", "recognize", "recognize_type1", "safe_consensus", "save_model",
    "score_confidence", "extract_features", "extract_features_type1", "predict_with_confidence",
    "TYPE1_FEATURE_LENGTH",
]
