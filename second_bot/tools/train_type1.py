#!/usr/bin/env python3
"""Train and benchmark the Type1 digit SVC while keeping legacy dataset helpers."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, __version__ as PILLOW_VERSION
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.svm import SVC

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from ..type1_ocr import build_model, recognize_type1, save_model
    from ..type1_ocr.features_type1 import (
        FEATURE_LENGTH,
        augment_canvas_features,
        extract_features,
        load_labeled_crops,
    )
    from ..type1_ocr.types import CELL_COUNT
except ImportError:
    from type1_ocr import build_model, recognize_type1, save_model
    from type1_ocr.features_type1 import FEATURE_LENGTH, augment_canvas_features, extract_features, load_labeled_crops
    from type1_ocr.types import CELL_COUNT


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    """Legacy reader for samples/label.csv; retained for existing callers/tests."""
    with (data_dir / "label.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("label.csv must contain filename,type,answer")
    selected, names, hashes = [], set(), {}
    for row in rows:
        if row.get("type", "").strip().lower() != "type1":
            continue
        name, answer = row.get("filename", "").strip(), row.get("answer", "").strip()
        path = Path(name)
        if not name or path.is_absolute() or len(path.parts) != 1 or name in {".", ".."}:
            raise ValueError(f"invalid image filename: {name!r}")
        if len(answer) != CELL_COUNT or not answer.isascii() or not answer.isdigit():
            raise ValueError(f"expected five ASCII digits for {name}")
        image_path = data_dir / name
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if name in names or digest in hashes:
            raise ValueError(f"duplicate filename/content: {name}")
        names.add(name)
        hashes[digest] = name
        selected.append({"filename": name, "answer": answer, "batch_id": row.get("batch_id", "").strip(), "type": "type1"})
    if not selected:
        raise ValueError("no type1 rows found")
    return selected


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def split_rows(rows: list[dict[str, str]], fraction: float, seed: int) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    """Legacy image-level split helper retained for compatibility."""
    if not 0.05 <= fraction <= 0.4:
        raise ValueError("validation fraction must be between 0.05 and 0.4")
    groups: dict[str, list[dict[str, str]]] = {}
    if all(row.get("batch_id") for row in rows):
        for row in rows:
            groups.setdefault(row["batch_id"], []).append(row)
        if len(groups) >= 2:
            target = max(1, round(len(rows) * fraction))
            holdout = min(groups.values(), key=lambda group: abs(len(group) - target))
            names = {row["filename"] for row in holdout}
            return [row for row in rows if row["filename"] not in names], holdout, "batch_holdout"
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    count = max(1, min(len(rows) - 1, round(len(rows) * fraction)))
    return shuffled[count:], shuffled[:count], "random_image_holdout_no_batch_id"


def evaluate(rows: list[dict[str, str]], data_dir: Path, model: dict[str, Any]) -> dict[str, Any]:
    """Legacy evaluator retained for callers using the samples/ dataset."""
    results, exact, correct, total, abstentions = [], 0, 0, 0, 0
    for row in rows:
        result = recognize_type1(data_dir / row["filename"], model)
        predicted = result.text
        exact += int(predicted == row["answer"])
        correct += sum(a == b for a, b in zip(predicted, row["answer"]))
        total += CELL_COUNT
        abstentions += int(result.abstained)
        results.append({"filename": row["filename"], "expected": row["answer"], "predicted": predicted, "confidence": result.confidence, "digit_confidence": list(result.digit_confidence), "abstained": result.abstained, "reason": result.reason})
    return {"sources": len(rows), "exact_sequences": exact, "exact_sequence_accuracy": exact / max(1, len(rows)), "correct_digits": correct, "total_digits": total, "digit_accuracy": correct / max(1, total), "abstentions": abstentions, "per_digit_confidence": results}


def split_crop_samples(samples: list[dict[str, Any]], seed: int = 41) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Select a stratified 20% fold with groups keyed by source captcha image."""
    if len({sample["group"] for sample in samples}) < 5:
        raise ValueError("at least five distinct CAPTCHA source groups are required")
    labels = np.asarray([sample["label"] for sample in samples])
    groups = np.asarray([sample["group"] for sample in samples])
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
    folds = list(splitter.split(np.zeros(len(samples)), labels, groups))
    target_samples = 0.2 * len(samples)
    target_groups = 0.2 * len(set(groups))
    train_indices, validation_indices = min(
        folds,
        key=lambda pair: (
            abs(len(pair[1]) - target_samples),
            abs(len(set(groups[pair[1]])) - target_groups),
        ),
    )
    train_groups, validation_groups = set(groups[train_indices]), set(groups[validation_indices])
    if train_groups & validation_groups:
        raise AssertionError("CAPTCHA source group leakage between training and validation")
    metadata = {
        "method": "StratifiedGroupKFold",
        "seed": seed,
        "validation_fraction_target": 0.2,
        "training_captchas": len(train_groups),
        "validation_captchas": len(validation_groups),
        "training_samples": int(len(train_indices)),
        "validation_samples": int(len(validation_indices)),
        "group_overlap": 0,
    }
    return train_indices, validation_indices, metadata


def make_classifier():
    """Construct the fast uncalibrated RBF SVC; confidence uses margins."""
    return SVC(kernel="rbf", C=0.5, gamma="scale", probability=False, random_state=41)


def _softmax_confidence(decision: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    scores = np.asarray(decision, dtype=np.float64)
    if scores.ndim == 1:
        scores = scores[:, None]
    shifted = scores - scores.max(axis=1, keepdims=True)
    probabilities = np.exp(np.clip(shifted, -50.0, 0.0))
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    order = np.sort(probabilities, axis=1)[:, ::-1]
    return probabilities, order[:, 0] - order[:, 1]


def prune_training_samples(samples: list[dict[str, Any]], train_indices: np.ndarray, seed: int = 41) -> tuple[np.ndarray, dict[str, Any]]:
    """Discard only strong OOF contradictions using grouped five-fold CV."""
    subset = np.asarray(train_indices, dtype=int)
    X = np.asarray([samples[int(i)]["features"] for i in subset], dtype=np.float32)
    y = np.asarray([samples[int(i)]["label"] for i in subset], dtype="U1")
    groups = np.asarray([samples[int(i)]["group"] for i in subset])
    oof_pred = np.empty(len(subset), dtype="U1")
    oof_margin = np.zeros(len(subset), dtype=np.float32)
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed + 1)
    for fit_pos, test_pos in splitter.split(X, y, groups):
        classifier = make_classifier()
        classifier.fit(X[fit_pos], y[fit_pos])
        decision = classifier.decision_function(X[test_pos])
        oof_pred[test_pos] = classifier.classes_[np.argmax(decision, axis=1)].astype("U1")
        _, margins = _softmax_confidence(decision)
        oof_margin[test_pos] = margins.astype(np.float32)
    strong_disagreement = (oof_pred != y) & (oof_margin >= 0.16)
    kept = subset[~strong_disagreement]
    return kept, {"method": "grouped_5_fold_oof_margin", "before": int(len(subset)), "removed": int(strong_disagreement.sum()), "after": int(len(kept)), "threshold": 0.16}


def augment_brush_crops(crop, sample_index: int) -> list[Any]:
    """Create eight deterministic brush/noise variants in training space only."""
    source = np.asarray(crop.convert("L"), dtype=np.uint8)
    height, width = source.shape
    rng = np.random.default_rng(4100 + int(sample_index))
    variants = [crop.copy()]
    for variant in range(7):
        image = source.copy()
        # Alternate thin/thick brush strokes with the requested kernel sizes.
        kernel_size = (2, 2) if variant % 2 == 0 else (3, 3)
        kernel = np.ones(kernel_size, dtype=np.uint8)
        image = cv2.erode(image, kernel, iterations=1) if variant % 3 == 0 else cv2.dilate(image, kernel, iterations=1)

        angle = float(rng.uniform(-7.0, 7.0))
        shear = float(rng.uniform(-0.055, 0.055))
        center = (width / 2.0, height / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        matrix[0, 1] += shear
        matrix[0, 2] -= shear * height / 2.0
        image = cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)

        # Faint crossing lines and tiny speckles reproduce CAPTCHA background noise.
        overlay = image.copy()
        for _ in range(1 + (variant % 2)):
            x0, y0 = int(rng.integers(0, width)), int(rng.integers(0, height))
            x1, y1 = int(rng.integers(0, width)), int(rng.integers(0, height))
            cv2.line(overlay, (x0, y0), (x1, y1), int(rng.integers(180, 236)), 1, cv2.LINE_AA)
        image = cv2.addWeighted(image, 0.94, overlay, 0.06, 0.0)
        speck_count = max(1, int(width * height * 0.0025))
        for _ in range(speck_count):
            x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
            image[y, x] = int(rng.integers(150, 245))
        variants.append(Image.fromarray(image, mode="L"))
    return variants


def fit_augmented(samples: list[dict[str, Any]], indices: Iterable[int]):
    features: list[np.ndarray] = []
    labels: list[str] = []
    for sample_index in indices:
        sample = samples[int(sample_index)]
        variants = augment_brush_crops(sample["image"], int(sample_index))
        features.extend(extract_features(variant) for variant in variants)
        labels.extend([sample["label"]] * len(variants))
    X = np.asarray(features, dtype=np.float32)
    y = np.asarray(labels, dtype="U1")
    if X.ndim != 2 or X.shape[1] != FEATURE_LENGTH:
        raise AssertionError(f"expected training features with shape (n,{FEATURE_LENGTH}), got {X.shape}")
    classifier = make_classifier()
    classifier.fit(X, y)
    return classifier, X.shape


def evaluate_crop_model(classifier, samples: list[dict[str, Any]], indices: np.ndarray) -> dict[str, Any]:
    X = np.asarray([samples[int(index)]["features"] for index in indices], dtype=np.float32)
    y_true = np.asarray([samples[int(index)]["label"] for index in indices], dtype="U1")
    y_pred = classifier.predict(X).astype(str)
    correct_by_digit = Counter()
    total_by_digit = Counter(y_true.tolist())
    confusion = Counter()
    for actual, predicted in zip(y_true.tolist(), y_pred.tolist()):
        if actual == predicted:
            correct_by_digit[actual] += 1
        else:
            confusion[(actual, predicted)] += 1
    per_digit = {
        digit: {
            "correct": int(correct_by_digit[digit]),
            "total": int(total_by_digit[digit]),
            "accuracy": correct_by_digit[digit] / total_by_digit[digit] if total_by_digit[digit] else None,
        }
        for digit in "0123456789"
    }

    by_group: dict[str, dict[int, tuple[dict[str, Any], str]]] = defaultdict(dict)
    for index, predicted in zip(indices.tolist(), y_pred.tolist()):
        sample = samples[int(index)]
        if sample["position"] in range(1, 6):
            by_group[sample["group"]][sample["position"]] = (sample, predicted)
    full_total = full_correct = 0
    for positions in by_group.values():
        if set(positions) != {1, 2, 3, 4, 5}:
            continue
        truth_labels = {positions[pos][0]["captcha_label"] for pos in range(1, 6)}
        expected_from_crops = "".join(positions[pos][0]["label"] for pos in range(1, 6))
        expected = next(iter(truth_labels)) if len(truth_labels) == 1 and next(iter(truth_labels)) else expected_from_crops
        if len(expected) != 5:
            continue
        predicted_code = "".join(positions[pos][1] for pos in range(1, 6))
        full_total += 1
        full_correct += int(predicted_code == expected)

    top_pairs = [
        {"true_digit": actual, "predicted_digit": predicted, "count": int(count)}
        for (actual, predicted), count in confusion.most_common(3)
    ]
    return {
        "validation_samples": int(len(indices)),
        "overall_digit_accuracy": float(np.mean(y_true == y_pred)) if len(y_true) else None,
        "per_digit_accuracy": per_digit,
        "full_captcha_total": full_total,
        "full_captcha_correct": full_correct,
        "full_captcha_accuracy": full_correct / full_total if full_total else None,
        "top_3_confused_pairs": top_pairs,
    }


def export_svc_artifact(classifier: SVC, output: Path) -> int:
    """Export RBF SVC parameters, quantizing support vectors for low storage."""
    support_vectors = np.asarray(classifier.support_vectors_, dtype=np.float32)
    feature_min = support_vectors.min(axis=0)
    feature_span = support_vectors.max(axis=0) - feature_min
    feature_scale = np.where(feature_span > 0, feature_span / 255.0, 1.0).astype(np.float32)
    quantized_vectors = np.rint((support_vectors - feature_min) / feature_scale).clip(0, 255).astype(np.uint8)
    arrays = {
        "support_vectors_": quantized_vectors,
        "_support_vector_scale_": feature_scale,
        "_support_vector_min_": feature_min.astype(np.float32),
        "dual_coef_": np.asarray(classifier.dual_coef_, dtype=np.float32),
        "intercept_": np.asarray(classifier.intercept_, dtype=np.float64),
        "_gamma": np.asarray(classifier._gamma, dtype=np.float64),
        "classes_": np.asarray(classifier.classes_, dtype="U1"),
        "n_support_": np.asarray(classifier.n_support_, dtype=np.int32),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        np.savez_compressed(handle, **arrays)
    return output.stat().st_size


def train_crops_and_report(data_dir: Path, output: Path, report_path: Path, seed: int = 41) -> dict[str, Any]:
    samples, skipped = load_labeled_crops(data_dir)
    if not samples:
        raise ValueError(f"no usable digit crops found in {data_dir}")
    train_indices, validation_indices, split_info = split_crop_samples(samples, seed=seed)
    # OOF pruning is intentionally disabled: rare brush/font variants are valid.
    pruned_train_indices = train_indices
    pruning = {"method": "disabled", "before": int(len(train_indices)), "removed": 0, "after": int(len(train_indices))}
    print(f"OOF pruning disabled; retaining {len(train_indices)} grouped training crops", flush=True)
    validation_classifier, augmented_shape = fit_augmented(samples, train_indices)
    metrics = evaluate_crop_model(validation_classifier, samples, validation_indices)
    print("Validation metrics computed; fitting final classifier on all usable crops...", flush=True)

    final_indices = range(len(samples))
    final_classifier, final_shape = fit_augmented(samples, final_indices)
    model_size = export_svc_artifact(final_classifier, output)

    report = {
        "dataset": str(data_dir),
        "total_usable_crops": len(samples),
        "skipped_outliers_by_digit": skipped,
        "split": split_info,
        "pruning": pruning,
        "validation_metrics": metrics,
        "augmentation": {
            "variants_per_crop": 8,
            "transforms": ["original", "erode/dilate 2x2 or 3x3", "rotation -7..+7 degrees", "horizontal shear +/-0.055", "white-border affine warp", "faint crossing lines", "tiny speckles", "deterministic mixed brush variant"],
            "validation_training_matrix_shape": list(augmented_shape),
            "final_training_matrix_shape": list(final_shape),
        },
        "classifier": "SVC(kernel='rbf', C=0.5, gamma='scale', probability=False)",
        "feature_length": FEATURE_LENGTH,
        "latency_ms": None,
        "model_path": str(output),
        "model_size_bytes": model_size,
        "model_under_2mb": model_size < 2 * 1024 * 1024,
        "pillow_version": PILLOW_VERSION,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _legacy_main(data_dir: Path, output: Path, report_path: Path, baseline_path: Path) -> int:
    rows = read_dataset(data_dir)
    model = build_model(rows, data_dir)
    after = evaluate(rows, data_dir, model)
    before = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    gate_passed = before is None or (after["exact_sequence_accuracy"] >= before["exact_sequence_accuracy"] and after["digit_accuracy"] >= before["digit_accuracy"])
    report = {
        "before": {k: before.get(k) for k in ("sources", "exact_sequences", "exact_sequence_accuracy", "digit_accuracy", "abstentions")} if before else None,
        "after": {k: after[k] for k in ("sources", "exact_sequences", "exact_sequence_accuracy", "digit_accuracy", "abstentions")},
        "model": {"classifier_kind": model.get("classifier_kind"), "training_sources": model.get("training_sources"), "sample_count": model.get("sample_count")},
        "gate_passed": gate_passed,
        "pillow_version": PILLOW_VERSION,
        "per_digit_confidence": after["per_digit_confidence"],
    }
    if not gate_passed:
        report["gate_reason"] = "Refused promotion because accuracy is lower than the original baseline."
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    save_model(model, output)
    report["model_size_bytes"] = output.stat().st_size
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_digit_confidence"}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[2]
    package_dir = Path(__file__).resolve().parents[1]
    parser.add_argument("--data", type=Path, default=root / "digits_crops")
    parser.add_argument("--output", type=Path, default=package_dir / "models" / "type1_model.npz")
    parser.add_argument("--report", type=Path, default=package_dir / "type1_sklearn_benchmark.json")
    parser.add_argument("--baseline", type=Path, default=root / "baseline_type1_benchmark.json")
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args(argv)

    if (args.data / "manifest.csv").is_file():
        report = train_crops_and_report(args.data, args.output, args.report, seed=args.seed)
        metrics = report["validation_metrics"]
        print(f"Validation single-digit accuracy: {metrics['overall_digit_accuracy']:.4%}")
        print("Per-digit accuracy:")
        for digit, values in metrics["per_digit_accuracy"].items():
            print(f"  {digit}: {values['accuracy']:.4%} ({values['correct']}/{values['total']})")
        captcha_accuracy = metrics["full_captcha_accuracy"]
        print(f"Full-captcha accuracy: {captcha_accuracy:.4%} ({metrics['full_captcha_correct']}/{metrics['full_captcha_total']})")
        print("Top 3 confused digit pairs:")
        for pair in metrics["top_3_confused_pairs"]:
            print(f"  {pair['true_digit']} -> {pair['predicted_digit']}: {pair['count']}")
        print(f"Model saved: {report['model_path']} ({report['model_size_bytes']} bytes; under 2 MB: {report['model_under_2mb']})")
        print(f"Validation report: {args.report}")
        return 0
    return _legacy_main(args.data, args.output, args.report, args.baseline)


if __name__ == "__main__":
    raise SystemExit(main())
