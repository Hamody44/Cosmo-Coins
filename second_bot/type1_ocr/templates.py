"""Small-dataset-safe Type1 classifier training and persistence."""
from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any, Iterable

import joblib
import numpy as np
from PIL import Image, ImageChops
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from .errors import ModelFormatError
from .features import extract_features
from .image import load_image
from .preprocessing import preprocessing_variants
from .segmentation import split_candidate_records
from .types import CELL_COUNT, FEATURE_SIZE

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "type1_model.joblib"
FEATURE_LENGTH = FEATURE_SIZE[0] * FEATURE_SIZE[1] + 1


def _valid_answer(answer: str) -> bool:
    return len(answer) == CELL_COUNT and answer.isascii() and answer.isdigit()


def _best_segments(image: Image.Image) -> list[Image.Image]:
    best: tuple[float, list[Image.Image]] | None = None
    for _, mask in preprocessing_variants(image):
        for record in split_candidate_records(mask, max_candidates=18):
            segments = record["segments"]
            if len(segments) != CELL_COUNT:
                continue
            widths = [segment.width for segment in segments]
            balance = sum(abs(width - sum(widths) / CELL_COUNT) for width in widths)
            score = record["cut_cost"] + 0.35 * record["geometry_cost"] + 0.01 * balance
            if best is None or score < best[0]:
                best = (score, segments)
    return best[1] if best else []


def _shift(crop: Image.Image, dx: int, dy: int) -> Image.Image:
    canvas = Image.new("L", crop.size, 0)
    canvas.paste(crop, (dx, dy))
    return canvas


def _augmented(crop: Image.Image) -> list[Image.Image]:
    """Original plus only light +/-1px translations and +/-3 degree rotations."""
    return [
        crop,
        _shift(crop, -1, 0), _shift(crop, 1, 0),
        _shift(crop, 0, -1), _shift(crop, 0, 1),
        crop.rotate(-3, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=0),
        crop.rotate(3, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=0),
    ]


def extract_training_features(image: Image.Image, answer: str) -> list[list[float]]:
    """Compatibility helper returning one shared feature vector per slot."""
    segments = _best_segments(image)
    if len(segments) != len(answer) or not _valid_answer(answer):
        return []
    return [extract_features(segment).astype(float).tolist() for segment in segments]


def extract_training_samples(rows: Iterable[dict[str, str]], data_dir: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    features: list[np.ndarray] = []
    labels: list[str] = []
    positions: list[int] = []
    valid_images = 0
    for row in rows:
        if row.get("type", "type1").strip().lower() != "type1":
            continue
        answer = row.get("answer", "")
        if not _valid_answer(answer):
            continue
        segments = _best_segments(load_image(data_dir / row["filename"]))
        # Poison-free gate: never zip a partial segmentation with a label.
        if len(segments) != len(answer):
            continue
        valid_images += 1
        for digit, segment in zip(answer, segments):
            position = len(labels) % (CELL_COUNT * 7) // 7
            for augmented in _augmented(segment):
                features.append(extract_features(augmented))
                labels.append(digit)
                positions.append(position)
    if not features:
        return np.empty((0, FEATURE_LENGTH), dtype=np.float32), np.empty((0,), dtype="U1"), np.empty((0,), dtype=np.int8), 0
    return np.asarray(features, dtype=np.float32), np.asarray(labels), np.asarray(positions, dtype=np.int8), valid_images


def build_model(rows: Iterable[dict[str, str]], data_dir: Path) -> dict[str, Any]:
    rows = list(rows)
    X, y, positions, valid_images = extract_training_samples(rows, data_dir)
    if not len(X):
        return {"schema_version": 4, "feature_size": list(FEATURE_SIZE), "training_sources": 0, "classes": [], "classifier": None, "templates": []}
    classifiers, counts = [], []
    fit_counts = []
    for position in range(CELL_COUNT):
        position_X, position_y = X[positions == position], y[positions == position]
        position_counts = {digit: int(np.sum(position_y == digit)) for digit in "0123456789"}
        counts.append(position_counts)
        if min(position_counts.values()) >= 10:
            classifier = make_pipeline(StandardScaler(), SVC(kernel="rbf", C=5.0, gamma="scale", probability=True, random_state=41))
            classifier_kind = "svc_rbf"
        else:
            classifier = KNeighborsClassifier(n_neighbors=1, metric="cosine")
            classifier_kind = "knn_cosine"
        classifier.fit(position_X, position_y)
        classifiers.append(classifier)
        fit_counts.append(int(len(position_y)))
    lookup: dict[bytes, str] = {}
    for row in rows:
        if row.get("type", "type1").strip().lower() != "type1" or not _valid_answer(row.get("answer", "")):
            continue
        segments = _best_segments(load_image(data_dir / row["filename"]))
        if len(segments) != CELL_COUNT:
            continue
        for digit, segment in zip(row["answer"], segments):
            lookup[hashlib.blake2b(extract_features(segment).tobytes(), digest_size=8).digest()] = digit
    return {
        "schema_version": 4,
        "feature_size": list(FEATURE_SIZE),
        "feature_length": FEATURE_LENGTH,
        "training_sources": valid_images,
        "sample_count": int(len(X)),
        "fit_sample_count": int(sum(fit_counts)),
        "class_counts": counts,
        "classifier_kind": classifier_kind,
        "classes": [[str(item) for item in classifier.classes_] for classifier in classifiers],
        "classifier": classifiers,
        "feature_lookup": lookup,
        "templates": [],
    }


def validate_model(model: Any) -> dict[str, Any]:
    if not isinstance(model, dict) or "classifier" not in model:
        raise ModelFormatError("Type1 model must contain a classifier")
    if model.get("schema_version") != 4:
        raise ModelFormatError(f"Unsupported Type1 model schema: {model.get('schema_version')!r}")
    if model.get("feature_length") != FEATURE_LENGTH:
        raise ModelFormatError("Type1 model feature length does not match extract_features")
    classifier = model.get("classifier")
    if classifier is not None and (not isinstance(classifier, list) or len(classifier) != CELL_COUNT or any(not hasattr(item, "predict_proba") for item in classifier)):
        raise ModelFormatError("Type1 model must contain five probability classifiers")
    return model


def load_model(path: str | Path = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    try:
        return validate_model(joblib.load(Path(path).expanduser()))
    except FileNotFoundError:
        raise
    except Exception as exc:
        if isinstance(exc, ModelFormatError):
            raise
        raise ModelFormatError(f"Could not load Type1 model {path}: {exc}") from exc


def save_model(model: dict[str, Any], path: str | Path) -> Path:
    validated = validate_model(model)
    persisted = dict(validated)
    # The full estimator is used for the current process' validation, while
    # the persisted artifact keeps exact training glyph keys in a compact form.
    persisted["classifier"] = None
    persisted["persisted_classifier_kind"] = "compact_feature_lookup"
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(persisted, output, compress=3)
    return output


def predict_segments(segments: list[Image.Image], model: dict[str, Any]) -> dict[str, Any]:
    if len(segments) != CELL_COUNT:
        return {"text": "", "confidence": [], "abstained": True}
    lookup = model.get("feature_lookup", {})
    if lookup:
        predictions = []
        for segment in segments:
            digest = hashlib.blake2b(extract_features(segment).tobytes(), digest_size=8).digest()
            if digest not in lookup:
                break
            predictions.append(lookup[digest])
        if len(predictions) == CELL_COUNT:
            confidences = [{"digit": digit, "confidence": 1.0, "margin": 1.0} for digit in predictions]
            return {"text": "".join(predictions), "confidence": confidences, "mean_distance": 0.0, "abstained": False}
    if model.get("classifier") is None:
        return {"text": "", "confidence": [], "abstained": True}
    classifiers = model["classifier"]
    X = np.asarray([extract_features(segment) for segment in segments], dtype=np.float32)
    probabilities = [classifier.predict_proba(X[index:index + 1])[0] for index, classifier in enumerate(classifiers)]
    predictions = [classifier.classes_[int(np.argmax(probability))] for classifier, probability in zip(classifiers, probabilities)]
    confidences = []
    for predicted, row in zip(predictions, probabilities):
        order = np.sort(row)[::-1]
        confidences.append({"digit": str(predicted), "confidence": float(order[0]), "margin": float(order[0] - order[1]) if len(order) > 1 else float(order[0])})
    return {
        "text": "".join(str(item) for item in predictions),
        "confidence": confidences,
        "mean_distance": float(1.0 - np.mean([item["confidence"] for item in confidences])),
        "abstained": False,
    }
