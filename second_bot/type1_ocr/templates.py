"""NumPy-only Type1 OCR inference and lightweight model persistence."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
from typing import Any, Iterable

import numpy as np
from PIL import Image

from .errors import ModelFormatError
from .features import extract_features
from .features_type1 import FEATURE_LENGTH as TYPE1_FEATURE_LENGTH, extract_features as extract_features_type1
from .image import load_image
from .preprocessing import preprocessing_variants
from .segmentation import split_candidate_records
from .types import CELL_COUNT, FEATURE_SIZE

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "type1_model.npz"
FEATURE_LENGTH = FEATURE_SIZE[0] * FEATURE_SIZE[1] + 1
_SVC_KEYS = {"support_vectors_", "dual_coef_", "intercept_", "_gamma", "classes_", "n_support_"}


def _is_numpy_svc(model: Any) -> bool:
    return isinstance(model, dict) and model.get("model_kind") == "rbf_ovo"


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
    """Original plus light +/-1px translations and +/-3 degree rotations."""
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
    """Build the legacy nearest-feature helper model without external ML packages."""
    rows = list(rows)
    X, y, positions, valid_images = extract_training_samples(rows, data_dir)
    if not len(X):
        return {"schema_version": 4, "feature_size": list(FEATURE_SIZE), "training_sources": 0, "classes": [], "classifier": None, "templates": []}
    classifiers: list[dict[str, np.ndarray]] = []
    counts = []
    for position in range(CELL_COUNT):
        position_X, position_y = X[positions == position], y[positions == position]
        counts.append({digit: int(np.sum(position_y == digit)) for digit in "0123456789"})
        classifiers.append({"features": position_X.astype(np.float32), "labels": position_y.astype("U1")})
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
        "class_counts": counts,
        "classes": [sorted(set(classifier["labels"].tolist())) for classifier in classifiers],
        "classifier": classifiers,
        "feature_lookup": lookup,
        "templates": [],
    }


def validate_model(model: Any) -> Any:
    if _is_numpy_svc(model):
        if not _SVC_KEYS.issubset(model):
            raise ModelFormatError("NumPy RBF model is missing required SVC parameters")
        vectors = np.asarray(model["support_vectors_"])
        coefficients = np.asarray(model["dual_coef_"])
        intercept = np.asarray(model["intercept_"])
        classes = np.asarray(model["classes_"])
        support_counts = np.asarray(model["n_support_"])
        pair_count = len(classes) * (len(classes) - 1) // 2
        if vectors.ndim != 2 or vectors.shape[1] != TYPE1_FEATURE_LENGTH:
            raise ModelFormatError("Type1 model feature length does not match extract_features_type1")
        if coefficients.shape != (len(classes) - 1, len(vectors)):
            raise ModelFormatError("Invalid RBF model dual coefficient shape")
        if intercept.shape != (pair_count,) or support_counts.shape != (len(classes),):
            raise ModelFormatError("Invalid RBF model class or intercept shape")
        if int(support_counts.sum()) != len(vectors) or not np.isfinite(float(model["_gamma"])) or float(model["_gamma"]) <= 0:
            raise ModelFormatError("Invalid RBF model support vectors or gamma")
        if np.issubdtype(vectors.dtype, np.integer):
            if "_support_vector_scale_" not in model or "_support_vector_min_" not in model:
                raise ModelFormatError("Quantized Type1 model is missing support-vector scale metadata")
            scale = np.asarray(model["_support_vector_scale_"], dtype=np.float32)
            offset = np.asarray(model["_support_vector_min_"], dtype=np.float32)
            if scale.shape != (vectors.shape[1],) or offset.shape != scale.shape or np.any(scale <= 0):
                raise ModelFormatError("Invalid quantized support-vector scale metadata")
        if set(str(item) for item in classes) != set("0123456789"):
            raise ModelFormatError("Type1 model must contain all ten digit classes")
        return model
    if not isinstance(model, dict) or "classifier" not in model:
        raise ModelFormatError("Type1 model must contain a classifier")
    if model.get("schema_version") != 4:
        raise ModelFormatError(f"Unsupported Type1 model schema: {model.get('schema_version')!r}")
    if model.get("feature_length") != FEATURE_LENGTH:
        raise ModelFormatError("Type1 model feature length does not match extract_features")
    classifier = model.get("classifier")
    if classifier is not None and (
        not isinstance(classifier, list)
        or len(classifier) != CELL_COUNT
        or any(not isinstance(item, dict) or not {"features", "labels"}.issubset(item) for item in classifier)
    ):
        raise ModelFormatError("Type1 model must contain five NumPy nearest-feature classifiers")
    return model


def _pack_legacy_model(model: dict[str, Any]) -> dict[str, np.ndarray]:
    metadata = {key: value for key, value in model.items() if key not in {"classifier", "feature_lookup"}}
    metadata["model_kind"] = "legacy_nearest"
    metadata["feature_lookup"] = [[key.hex(), value] for key, value in model.get("feature_lookup", {}).items()]
    arrays: dict[str, np.ndarray] = {"metadata": np.asarray(json.dumps(metadata))}
    for index, classifier in enumerate(model.get("classifier") or []):
        arrays[f"classifier_{index}_features"] = np.asarray(classifier["features"], dtype=np.float32)
        arrays[f"classifier_{index}_labels"] = np.asarray(classifier["labels"], dtype="U1")
    return arrays


def load_model(path: str | Path = DEFAULT_MODEL_PATH) -> Any:
    try:
        with np.load(Path(path).expanduser(), allow_pickle=False) as archive:
            keys = set(archive.files)
            if "metadata" in keys:
                metadata = json.loads(str(archive["metadata"].item()))
                metadata["feature_lookup"] = {bytes.fromhex(key): value for key, value in metadata.get("feature_lookup", [])}
                if metadata.get("model_kind") == "legacy_nearest":
                    classifiers = []
                    for index in range(CELL_COUNT):
                        feature_key, label_key = f"classifier_{index}_features", f"classifier_{index}_labels"
                        if feature_key not in keys or label_key not in keys:
                            break
                        classifiers.append({"features": archive[feature_key].copy(), "labels": archive[label_key].astype("U1")})
                    metadata["classifier"] = classifiers or None
                return validate_model(metadata)
            missing = _SVC_KEYS - keys
            if missing:
                raise ModelFormatError(f"NumPy model is missing arrays: {', '.join(sorted(missing))}")
            model = {key: archive[key].copy() for key in _SVC_KEYS if key != "_gamma"}
            for optional_key in ("_support_vector_scale_", "_support_vector_min_"):
                if optional_key in keys:
                    model[optional_key] = archive[optional_key].copy()
            model["_gamma"] = float(archive["_gamma"].item())
            model["model_kind"] = "rbf_ovo"
            return validate_model(model)
    except FileNotFoundError:
        raise
    except Exception as exc:
        if isinstance(exc, ModelFormatError):
            raise
        raise ModelFormatError(f"Could not load Type1 model {path}: {exc}") from exc


def save_model(model: dict[str, Any], path: str | Path) -> Path:
    validated = validate_model(model)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if _is_numpy_svc(validated):
        arrays = {key: np.asarray(validated[key]) for key in _SVC_KEYS}
        for optional_key in ("_support_vector_scale_", "_support_vector_min_"):
            if optional_key in validated:
                arrays[optional_key] = np.asarray(validated[optional_key])
    else:
        arrays = _pack_legacy_model(validated)
    with output.open("wb") as handle:
        np.savez_compressed(handle, **arrays)
    return output


def _softmax_confidence(decision: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    scores = np.asarray(decision, dtype=np.float64)
    if scores.ndim == 1:
        scores = scores[:, None]
    shifted = scores - scores.max(axis=1, keepdims=True)
    probabilities = np.exp(np.clip(shifted, -50.0, 0.0))
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    order = np.sort(probabilities, axis=1)[:, ::-1]
    margins = order[:, 0] - (order[:, 1] if order.shape[1] > 1 else 0.0)
    return probabilities, margins


def _predict_rbf_ovo(X: np.ndarray, model: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized RBF kernel and one-vs-one votes/margins, implemented in NumPy."""
    vectors = np.asarray(model["support_vectors_"])
    if np.issubdtype(vectors.dtype, np.integer):
        vectors = vectors.astype(np.float32) * np.asarray(model["_support_vector_scale_"], dtype=np.float32)
        vectors += np.asarray(model["_support_vector_min_"], dtype=np.float32)
    else:
        vectors = vectors.astype(np.float32, copy=False)
    samples = np.asarray(X, dtype=np.float32)
    vector_norms = np.einsum("ij,ij->i", vectors, vectors, dtype=np.float64)
    sample_norms = np.einsum("ij,ij->i", samples, samples, dtype=np.float64)
    distances = sample_norms[:, None] + vector_norms[None, :] - 2.0 * (samples @ vectors.T)
    np.maximum(distances, 0.0, out=distances)
    kernel = np.exp(-float(model["_gamma"]) * distances)

    classes = np.asarray(model["classes_"])
    counts = np.asarray(model["n_support_"], dtype=np.int64)
    starts = np.concatenate(([0], np.cumsum(counts[:-1])))
    votes = np.zeros((len(samples), len(classes)), dtype=np.float64)
    margin_scores = np.zeros_like(votes)
    intercepts = np.asarray(model["intercept_"], dtype=np.float64)
    coefficients = np.asarray(model["dual_coef_"], dtype=np.float64)
    pair_index = 0
    for first in range(len(classes)):
        for second in range(first + 1, len(classes)):
            first_start, first_end = int(starts[first]), int(starts[first] + counts[first])
            second_start, second_end = int(starts[second]), int(starts[second] + counts[second])
            sv_indices = np.r_[np.arange(first_start, first_end), np.arange(second_start, second_end)]
            weights = np.concatenate((coefficients[second - 1, first_start:first_end], coefficients[first, second_start:second_end]))
            decision = kernel[:, sv_indices] @ weights + intercepts[pair_index]
            first_wins = decision > 0.0
            votes[:, first] += first_wins
            votes[:, second] += ~first_wins
            scaled_margin = decision / (3.0 * (np.abs(decision) + 1.0))
            margin_scores[:, first] += scaled_margin
            margin_scores[:, second] -= scaled_margin
            pair_index += 1
    class_scores = votes + margin_scores
    probabilities, _ = _softmax_confidence(class_scores)
    return np.argmax(class_scores, axis=1), probabilities


def predict_segments(segments: list[Image.Image], model: Any) -> dict[str, Any]:
    if len(segments) != CELL_COUNT:
        return {"text": "", "confidence": [], "abstained": True}
    if _is_numpy_svc(model):
        X = np.asarray([extract_features_type1(segment) for segment in segments], dtype=np.float32)
        prediction_indices, probabilities = _predict_rbf_ovo(X, model)
        predictions = np.asarray(model["classes_"])[prediction_indices]
        confidences = []
        for predicted, row in zip(predictions, probabilities):
            order = np.sort(row)[::-1]
            confidences.append({"digit": str(predicted), "confidence": float(order[0]), "margin": float(order[0] - order[1])})
        return {"text": "".join(str(item) for item in predictions), "confidence": confidences, "mean_distance": float(1.0 - np.mean([item["confidence"] for item in confidences])), "abstained": False}

    # Kept for external test doubles and the package's legacy compatibility API.
    if hasattr(model, "predict_proba") and getattr(model, "n_features_in_", None) == TYPE1_FEATURE_LENGTH:
        X = np.asarray([extract_features_type1(segment) for segment in segments], dtype=np.float32)
        probabilities = np.asarray(model.predict_proba(X), dtype=np.float64)
        predictions = np.asarray(model.classes_)[np.argmax(probabilities, axis=1)]
        rows = [probabilities[index] for index in range(CELL_COUNT)]
    else:
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
        classifiers = model.get("classifier")
        if classifiers is None:
            return {"text": "", "confidence": [], "abstained": True}
        X = np.asarray([extract_features(segment) for segment in segments], dtype=np.float32)
        probabilities, predictions = [], []
        for index, classifier in enumerate(classifiers):
            train_X, labels = np.asarray(classifier["features"], dtype=np.float32), np.asarray(classifier["labels"])
            distances = np.sum((train_X - X[index]) ** 2, axis=1)
            nearest_distance = distances.min()
            classes, counts = np.unique(labels[distances == nearest_distance], return_counts=True)
            selected = int(np.argmax(counts))
            row = np.zeros(len(classes), dtype=np.float64)
            row[selected] = 1.0
            probabilities.append(row)
            predictions.append(classes[selected])
        rows = probabilities
    confidences = []
    for predicted, row in zip(predictions, rows):
        order = np.sort(row)[::-1]
        confidences.append({"digit": str(predicted), "confidence": float(order[0]), "margin": float(order[0] - order[1]) if len(order) > 1 else float(order[0])})
    return {"text": "".join(str(item) for item in predictions), "confidence": confidences, "mean_distance": float(1.0 - np.mean([item["confidence"] for item in confidences])), "abstained": False}
