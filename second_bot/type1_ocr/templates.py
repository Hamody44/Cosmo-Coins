"""Template model construction, loading, validation, and glyph matching."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .errors import ModelFormatError
from .features import extract_feature, feature_distance
from .image import load_image
from .preprocessing import preprocessing_variants
from .segmentation import segment_candidates, split_candidate_records
from .types import CELL_COUNT, FEATURE_SIZE

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "type1_local_ocr.json"


def _valid_answer(answer: str) -> bool:
    return len(answer) == CELL_COUNT and answer.isascii() and answer.isdigit()


def extract_training_features(image: Image.Image, answer: str) -> list[list[float]]:
    """Choose a geometry-balanced split for one labelled image."""
    if not _valid_answer(answer):
        return []
    best: tuple[float, list[Image.Image]] | None = None
    for _, mask in preprocessing_variants(image):
        for segments in segment_candidates(mask, max_candidates=18):
            widths = [segment.width for segment in segments]
            score = sum(abs(width - sum(widths) / CELL_COUNT) for width in widths)
            if best is None or score < best[0]:
                best = (score, segments)
    if best is None:
        return []
    return [extract_feature(segment) for segment in best[1]]


def build_model(rows: Iterable[dict[str, str]], data_dir: Path) -> dict[str, Any]:
    """Build templates from Type1 rows only; rows without type are accepted for compatibility."""
    type1_rows = [row for row in rows if row.get("type", "type1").strip().lower() == "type1"]
    provisional: list[dict[str, Any]] = []
    prepared: list[tuple[dict[str, str], list[list[float]]]] = []
    for row in type1_rows:
        answer = row.get("answer", "")
        if not _valid_answer(answer):
            continue
        features = extract_training_features(load_image(data_dir / row["filename"]), answer)
        if len(features) != CELL_COUNT:
            continue
        prepared.append((row, features))
        for position, (digit, feature) in enumerate(zip(answer, features)):
            provisional.append({"position": position, "digit": digit, "feature": feature})

    selected_templates: list[dict[str, Any]] = []
    for row, _ in prepared:
        best: tuple[float, list[Image.Image]] | None = None
        for _, mask in preprocessing_variants(load_image(data_dir / row["filename"])):
            for record in split_candidate_records(mask, max_candidates=18):
                features = [extract_feature(segment) for segment in record["segments"]]
                if len(features) != CELL_COUNT:
                    continue
                template_cost = sum(
                    min((feature_distance(features[position], item["feature"])
                         for item in provisional
                         if item["position"] == position and item["digit"] == row["answer"][position]),
                        default=1.0)
                    for position in range(CELL_COUNT)
                ) / CELL_COUNT
                score = template_cost + 0.20 * record["cut_cost"] + 0.10 * record["geometry_cost"]
                if best is None or score < best[0]:
                    best = (score, record["segments"])
        if best is not None:
            for position, (digit, feature) in enumerate(zip(row["answer"], map(extract_feature, best[1]))):
                selected_templates.append({"position": position, "digit": digit, "feature": feature})
    return {
        "schema_version": 3,
        "feature_size": list(FEATURE_SIZE),
        "training_sources": len(selected_templates) // CELL_COUNT,
        "templates": selected_templates,
    }


def validate_model(model: Any) -> dict[str, Any]:
    if not isinstance(model, dict) or not isinstance(model.get("templates"), list):
        raise ModelFormatError("Type1 model must be an object with a templates list")
    if model.get("schema_version", 3) != 3:
        raise ModelFormatError(f"Unsupported Type1 model schema: {model.get('schema_version')!r}")
    for index, item in enumerate(model["templates"]):
        if not isinstance(item, dict):
            raise ModelFormatError(f"Type1 template #{index} is not an object")
        try:
            position = int(item["position"])
            digit = str(item["digit"])
            feature = item["feature"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelFormatError(f"Type1 template #{index} is incomplete") from exc
        if position not in range(CELL_COUNT) or len(digit) != 1 or digit not in "0123456789":
            raise ModelFormatError(f"Type1 template #{index} has an invalid class")
        expected_feature_length = FEATURE_SIZE[0] * FEATURE_SIZE[1] + FEATURE_SIZE[0] + FEATURE_SIZE[1]
        if (
            not isinstance(feature, list)
            or len(feature) != expected_feature_length
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in feature)
        ):
            raise ModelFormatError(f"Type1 template #{index} has an invalid feature vector")
    return model


def load_model(path: str | Path = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    model_path = Path(path).expanduser()
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelFormatError(f"Could not load Type1 model {model_path}: {exc}") from exc
    return validate_model(payload)


def save_model(model: dict[str, Any], path: str | Path) -> Path:
    validated = validate_model(model)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def predict_segments(segments: list[Image.Image], model: dict[str, Any]) -> dict[str, Any]:
    """Return nearest positional templates and per-character distances/margins."""
    # Public API validates the full payload once before evaluating candidates;
    # repeating that scan for every split is needlessly expensive.
    templates = model.get("templates", []) if isinstance(model, dict) else []
    text, confidences = "", []
    for position, segment in enumerate(segments):
        feature = extract_feature(segment)
        ranked: list[tuple[float, str]] = []
        for digit in "0123456789":
            candidates = [item for item in templates if int(item["position"]) == position and str(item["digit"]) == digit]
            distances = [feature_distance(feature, item["feature"]) for item in candidates if len(item["feature"]) == len(feature)]
            if distances:
                ranked.append((min(distances), digit))
        ranked.sort(key=lambda item: (item[0], item[1]))
        if not ranked:
            return {"text": "", "confidence": [], "abstained": True}
        best_distance = ranked[0][0]
        second_distance = ranked[1][0] if len(ranked) > 1 else best_distance + 1.0
        margin = max(0.0, second_distance - best_distance)
        text += ranked[0][1]
        confidences.append({"digit": ranked[0][1], "distance": best_distance, "margin": margin})
    return {
        "text": text,
        "confidence": confidences,
        "mean_distance": sum(item["distance"] for item in confidences) / CELL_COUNT,
        "abstained": False,
    }
