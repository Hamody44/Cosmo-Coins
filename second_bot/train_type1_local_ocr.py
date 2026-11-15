#!/usr/bin/env python3
"""Train and evaluate the experimental type1 sequence OCR.

Training/export only; install the optional dependencies from
cosmo_type1_training_requirements.txt. The Discord bot does not need PyTorch.

Example after collecting data:
    python3 train_type1_local_ocr.py --data ../samples --test-data ../samples_holdout

The test folder must be a separately collected, untouched batch. When both
CSV manifests include batch_id, export is allowed only if the held-out batch
is distinct from every training batch and the quality gates pass.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image, ImageEnhance, ImageFilter
from torch import nn
from torch.utils.data import DataLoader, Dataset

from type1_local_ocr import ctc_greedy_decode, load_image
from type1_sequence_model import (
    BLANK_INDEX,
    CELL_COUNT,
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    Type1SequenceOCR,
)

DIGITS = set("0123456789")


def read_dataset(data_dir: Path) -> list[dict[str, str]]:
    """Read and validate type1 rows; reject duplicate file content."""
    label_path = data_dir / "label.csv"
    with label_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"filename", "type", "answer"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{label_path} must contain columns {sorted(required)}")

    selected: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_hashes: dict[str, str] = {}
    for row in rows:
        if row.get("type", "").strip() != "type1":
            continue
        filename = row.get("filename", "").strip()
        answer = row.get("answer", "").strip()
        relative = Path(filename)
        if not filename or relative.is_absolute() or len(relative.parts) != 1 or filename in {".", ".."}:
            raise ValueError(f"Invalid image filename in {label_path}: {filename!r}")
        if filename in seen_names:
            raise ValueError(f"Duplicate filename in {label_path}: {filename}")
        if len(answer) != CELL_COUNT or not answer.isascii() or not answer.isdigit():
            raise ValueError(f"Expected exactly five ASCII digits for {filename}: {answer!r}")
        image_path = data_dir / filename
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest in seen_hashes:
            raise ValueError(
                f"Duplicate image content: {filename} and {seen_hashes[digest]}"
            )
        seen_names.add(filename)
        seen_hashes[digest] = filename
        selected.append({
            "filename": filename,
            "answer": answer,
            "batch_id": row.get("batch_id", "").strip(),
            "sha256": digest,
        })
    if not selected:
        raise ValueError(f"No type1 rows found in {label_path}")
    return selected


def split_train_validation(
    rows: list[dict[str, str]], *, fraction: float, seed: int
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    """Use a whole-batch holdout when batch_id is available; otherwise random image split."""
    if not 0.05 <= fraction <= 0.4:
        raise ValueError("validation fraction must be between 0.05 and 0.4")
    group_ids = {row.get("batch_id", "") for row in rows}
    if "" not in group_ids and len(group_ids) >= 2:
        groups: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            groups.setdefault(row["batch_id"], []).append(row)
        target = max(1, round(len(rows) * fraction))
        possible = [group for group in groups.values() if len(rows) - len(group) >= 1]
        validation_group = min(possible, key=lambda group: abs(len(group) - target))
        validation_ids = {row["filename"] for row in validation_group}
        train = [row for row in rows if row["filename"] not in validation_ids]
        validation = [row for row in rows if row["filename"] in validation_ids]
        return train, validation, "batch_holdout"

    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    validation_count = max(1, min(len(rows) - 1, round(len(rows) * fraction)))
    return shuffled[validation_count:], shuffled[:validation_count], "random_image_holdout"


def _augment(image: Image.Image, rng: random.Random) -> Image.Image:
    """Apply mild label-preserving variation without drawing synthetic characters."""
    if rng.random() < 0.8:
        image = ImageEnhance.Contrast(image).enhance(rng.uniform(0.82, 1.22))
    if rng.random() < 0.8:
        image = ImageEnhance.Brightness(image).enhance(rng.uniform(0.88, 1.12))
    if rng.random() < 0.25:
        image = image.filter(ImageFilter.GaussianBlur(rng.uniform(0.15, 0.45)))
    if rng.random() < 0.5:
        image = image.rotate(
            rng.uniform(-1.5, 1.5),
            resample=Image.Resampling.BILINEAR,
            fillcolor=255,
        )
    return image


class CaptchaDataset(Dataset):
    def __init__(self, rows: list[dict[str, str]], data_dir: Path, *, augment: bool, seed: int) -> None:
        self.rows = rows
        self.data_dir = data_dir
        self.augment = augment
        self.seed = seed

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int):
        row = self.rows[index]
        image = load_image(self.data_dir / row["filename"])
        if self.augment:
            rng = random.Random((self.seed << 20) + index + random.randrange(1 << 20))
            image = _augment(image, rng)
        array = np.asarray(image, dtype=np.float32) / 127.5 - 1.0
        tensor = torch.from_numpy(array[None, :, :])
        target = torch.tensor([int(char) + 1 for char in row["answer"]], dtype=torch.long)
        return tensor, target, row["filename"], row["answer"]


def collate_batch(batch):
    images = torch.stack([item[0] for item in batch])
    targets = torch.cat([item[1] for item in batch])
    target_lengths = torch.tensor([len(item[1]) for item in batch], dtype=torch.long)
    return images, targets, target_lengths, [item[2] for item in batch], [item[3] for item in batch]


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_char in enumerate(right, start=1):
            current.append(min(
                previous[column_index] + 1,
                current[column_index - 1] + 1,
                previous[column_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def evaluate_model(
    model: Type1SequenceOCR,
    loader: DataLoader,
    *,
    device: torch.device,
    criterion: nn.CTCLoss | None = None,
) -> dict[str, Any]:
    model.eval()
    total_exact = 0
    total_edits = 0
    total_characters = 0
    fixed_length = 0
    valid_exact = 0
    losses: list[float] = []
    records: list[dict[str, Any]] = []
    with torch.no_grad():
        for images, targets, target_lengths, filenames, answers in loader:
            images = images.to(device)
            sequence_logits, _ = model(images)
            if criterion is not None:
                log_probs = sequence_logits.log_softmax(-1).transpose(0, 1)
                input_lengths = torch.full(
                    (images.shape[0],), sequence_logits.shape[1], dtype=torch.long
                )
                losses.append(float(criterion(log_probs, targets, input_lengths, target_lengths).item()))
            logits = sequence_logits.detach().cpu().numpy()
            for filename, answer, row_logits in zip(filenames, answers, logits):
                decoded = ctc_greedy_decode(row_logits)
                predicted = decoded["text"]
                distance = edit_distance(answer, predicted)
                is_exact = predicted == answer
                total_exact += int(is_exact)
                total_edits += distance
                total_characters += len(answer)
                fixed_length += int(len(predicted) == CELL_COUNT)
                valid_exact += int(is_exact and len(predicted) == CELL_COUNT)
                records.append({
                    "filename": filename,
                    "expected": answer,
                    "predicted": predicted,
                    "exact": is_exact,
                    "edit_distance": distance,
                    "mean_confidence": float(decoded["mean_confidence"]),
                })
    sample_count = len(records)
    fixed_count = sum(record["predicted"].__len__() == CELL_COUNT for record in records)
    return {
        "sample_count": sample_count,
        "exact_correct": total_exact,
        "exact_accuracy": total_exact / max(1, sample_count),
        "character_error_rate": total_edits / max(1, total_characters),
        "character_accuracy": 1.0 - total_edits / max(1, total_characters),
        "five_digit_coverage": fixed_count / max(1, sample_count),
        "five_digit_precision": valid_exact / max(1, fixed_count),
        "mean_ctc_loss": sum(losses) / len(losses) if losses else None,
        "records": records,
    }


def calibrate_confidence_threshold(
    records: list[dict[str, Any]], *, target_precision: float, min_coverage: float
) -> float:
    """Choose a development-set threshold; return >1 to reject all if unsupported."""
    if not records:
        return 1.1
    candidates = sorted({float(row["mean_confidence"]) for row in records})
    candidates = [0.0, *candidates, 1.000001]
    best_threshold = 1.1
    best_count = 0
    for threshold in candidates:
        selected = [
            row for row in records
            if len(row["predicted"]) == CELL_COUNT
            and float(row["mean_confidence"]) >= threshold
        ]
        coverage = len(selected) / len(records)
        precision = sum(bool(row["exact"]) for row in selected) / max(1, len(selected))
        if coverage >= min_coverage and precision >= target_precision and len(selected) > best_count:
            best_threshold = threshold
            best_count = len(selected)
    return best_threshold


def threshold_metrics(records: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    selected = [
        row for row in records
        if len(row["predicted"]) == CELL_COUNT
        and float(row["mean_confidence"]) >= threshold
    ]
    correct = sum(bool(row["exact"]) for row in selected)
    return {
        "threshold": threshold,
        "accepted": len(selected),
        "coverage": len(selected) / max(1, len(records)),
        "correct": correct,
        "precision": correct / max(1, len(selected)),
    }


def wilson_lower_bound(successes: int, trials: int, *, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = proportion + z * z / (2 * trials)
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * trials)) / trials)
    return max(0.0, (center - margin) / denominator)


def train_model(
    model: Type1SequenceOCR,
    train_loader: DataLoader,
    validation_loader: DataLoader,
    *,
    epochs: int,
    patience: int,
    learning_rate: float,
    device: torch.device,
) -> tuple[Type1SequenceOCR, dict[str, Any]]:
    criterion = nn.CTCLoss(blank=BLANK_INDEX, zero_infinity=True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    best_score = float("-inf")
    best_state = None
    best_metrics: dict[str, Any] = {}
    stale_epochs = 0
    started = time.monotonic()

    for epoch in range(1, epochs + 1):
        model.train()
        batch_losses: list[float] = []
        for images, targets, target_lengths, _, _ in train_loader:
            images = images.to(device)
            targets = targets.to(device)
            optimizer.zero_grad(set_to_none=True)
            sequence_logits, position_logits = model(images)
            log_probs = sequence_logits.log_softmax(-1).transpose(0, 1)
            input_lengths = torch.full(
                (images.shape[0],), sequence_logits.shape[1], dtype=torch.long
            )
            ctc_loss = criterion(log_probs, targets, input_lengths, target_lengths)
            position_targets = targets.view(images.shape[0], CELL_COUNT) - 1
            position_loss = nn.functional.cross_entropy(
                position_logits.reshape(-1, 10), position_targets.reshape(-1)
            )
            loss = ctc_loss + 0.35 * position_loss
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            batch_losses.append(float(loss.detach().cpu().item()))

        validation = evaluate_model(model, validation_loader, device=device, criterion=criterion)
        score = validation["exact_accuracy"] * 5.0 + validation["character_accuracy"]
        print(
            f"epoch={epoch:03d} train_loss={sum(batch_losses) / max(1, len(batch_losses)):.4f} "
            f"dev_exact={validation['exact_accuracy']:.3f} "
            f"dev_char={validation['character_accuracy']:.3f}",
            flush=True,
        )
        if score > best_score:
            best_score = score
            best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
            best_metrics = validation
            stale_epochs = 0
        else:
            stale_epochs += 1
        if stale_epochs >= patience:
            break

    if best_state is not None:
        model.load_state_dict(best_state)
    best_metrics["epochs_completed"] = epoch
    best_metrics["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return model, best_metrics


def export_onnx(model: Type1SequenceOCR, output_path: Path) -> None:
    class SequenceHead(nn.Module):
        def __init__(self, inner: Type1SequenceOCR) -> None:
            super().__init__()
            self.inner = inner

        def forward(self, image):
            return self.inner(image)[0]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = SequenceHead(model).eval()
    dummy = torch.zeros(1, 1, IMAGE_HEIGHT, IMAGE_WIDTH, dtype=torch.float32)
    torch.onnx.export(
        wrapper,
        dummy,
        str(output_path),
        input_names=["image"],
        output_names=["sequence_logits"],
        dynamic_axes={"image": {0: "batch"}, "sequence_logits": {0: "batch"}},
        opset_version=18,
        do_constant_folding=True,
    )
    try:
        import onnxruntime as ort
    except ImportError as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("ONNX Runtime is required to verify an exported model") from exc
    session = ort.InferenceSession(str(output_path), providers=["CPUExecutionProvider"])
    output = session.run(None, {"image": np.zeros((1, 1, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.float32)})[0]
    if output.ndim != 3 or output.shape[0] != 1 or output.shape[2] != 11:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Unexpected ONNX output shape: {output.shape}")


def _batch_ids(rows: list[dict[str, str]]) -> set[str]:
    return {row.get("batch_id", "") for row in rows if row.get("batch_id", "")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent.parent / "samples")
    parser.add_argument("--test-data", type=Path, help="Separate, untouched test-data directory")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "models" / "type1_sequence_ocr.onnx")
    parser.add_argument("--report", type=Path, help="Optional JSON report destination")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--patience", type=int, default=14)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--learning-rate", type=float, default=0.001)
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--min-training-samples", type=int, default=800)
    parser.add_argument("--min-test-samples", type=int, default=200)
    parser.add_argument("--min-test-exact-accuracy", type=float, default=0.95)
    parser.add_argument("--min-wilson-lower-bound", type=float, default=0.90)
    parser.add_argument("--target-precision", type=float, default=0.99)
    parser.add_argument("--min-coverage", type=float, default=0.20)
    args = parser.parse_args()
    if args.epochs < 1 or args.patience < 1 or args.batch_size < 1:
        parser.error("epochs, patience, and batch-size must be positive")
    if not 0 < args.min_test_exact_accuracy <= 1:
        parser.error("min-test-exact-accuracy must be in (0,1]")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, args.threads))
    device = torch.device("cpu")

    try:
        all_rows = read_dataset(args.data)
        test_rows = read_dataset(args.test_data) if args.test_data else []
        training_hashes = {row["sha256"] for row in all_rows}
        if training_hashes.intersection(row["sha256"] for row in test_rows):
            raise ValueError("Training and test data contain identical image bytes")
        train_rows, dev_rows, split_kind = split_train_validation(
            all_rows, fraction=args.validation_fraction, seed=args.seed
        )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    train_loader = DataLoader(
        CaptchaDataset(train_rows, args.data, augment=True, seed=args.seed),
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=collate_batch,
    )
    dev_loader = DataLoader(
        CaptchaDataset(dev_rows, args.data, augment=False, seed=args.seed),
        batch_size=args.batch_size,
        shuffle=False,
        collate_fn=collate_batch,
    )
    model = Type1SequenceOCR().to(device)
    model, dev_metrics = train_model(
        model,
        train_loader,
        dev_loader,
        epochs=args.epochs,
        patience=args.patience,
        learning_rate=args.learning_rate,
        device=device,
    )
    confidence_threshold = calibrate_confidence_threshold(
        dev_metrics["records"],
        target_precision=args.target_precision,
        min_coverage=args.min_coverage,
    )

    report: dict[str, Any] = {
        "architecture": "CNN-BiLSTM-CTC with auxiliary fixed-position loss",
        "training_samples": len(train_rows),
        "development_samples": len(dev_rows),
        "development_split": split_kind,
        "development_batch_ids_present": bool(_batch_ids(train_rows) and _batch_ids(dev_rows)),
        "development": dev_metrics,
        "confidence_threshold_calibrated_on_development": confidence_threshold,
        "test": None,
        "test_acceptance": None,
        "exported": False,
    }

    if test_rows:
        test_loader = DataLoader(
            CaptchaDataset(test_rows, args.test_data, augment=False, seed=args.seed),
            batch_size=args.batch_size,
            shuffle=False,
            collate_fn=collate_batch,
        )
        test_metrics = evaluate_model(model, test_loader, device=device)
        calibrated = threshold_metrics(test_metrics["records"], confidence_threshold)
        train_batches = _batch_ids(all_rows)
        test_batches = _batch_ids(test_rows)
        all_rows_have_batch_ids = all(row.get("batch_id", "") for row in all_rows)
        test_rows_have_batch_ids = all(row.get("batch_id", "") for row in test_rows)
        independent_batches = bool(
            all_rows_have_batch_ids
            and test_rows_have_batch_ids
            and train_batches
            and test_batches
            and train_batches.isdisjoint(test_batches)
        )
        lower_bound = wilson_lower_bound(
            test_metrics["exact_correct"], test_metrics["sample_count"]
        )
        acceptance_checks = {
            "enough_training_images": len(all_rows) >= args.min_training_samples,
            "enough_test_images": test_metrics["sample_count"] >= args.min_test_samples,
            "independent_batch_ids": independent_batches,
            "exact_accuracy_target": test_metrics["exact_accuracy"] >= args.min_test_exact_accuracy,
            "wilson_lower_bound_target": lower_bound >= args.min_wilson_lower_bound,
            "calibrated_precision_target": calibrated["precision"] >= args.target_precision,
            "calibrated_coverage_target": calibrated["coverage"] >= args.min_coverage,
        }
        approved = all(acceptance_checks.values())
        report["test"] = {
            **test_metrics,
            "wilson_95_lower_bound": lower_bound,
            "calibrated_acceptance": calibrated,
        }
        report["test_acceptance"] = {"approved": approved, "checks": acceptance_checks}
        if approved:
            export_onnx(model, args.output)
            metadata = {
                "model_name": "cosmo-type1-sequence-ctc",
                "architecture": report["architecture"],
                "image_size": [IMAGE_WIDTH, IMAGE_HEIGHT],
                "blank_index": BLANK_INDEX,
                "confidence_threshold": confidence_threshold,
                "production_qualified": True,
                "qualification": {
                    "test_samples": test_metrics["sample_count"],
                    "exact_accuracy": test_metrics["exact_accuracy"],
                    "wilson_95_lower_bound": lower_bound,
                    "calibrated_precision": calibrated["precision"],
                    "calibrated_coverage": calibrated["coverage"],
                },
            }
            args.output.with_suffix(".json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["exported"] = True
        else:
            report["test_acceptance"]["reasons"] = [
                name for name, passed in acceptance_checks.items() if not passed
            ]
    else:
        report["test_acceptance"] = {
            "approved": False,
            "reasons": ["No separate untouched --test-data set was provided"],
        }

    printable = {key: value for key, value in report.items() if key not in {"development", "test"}}
    printable["development_summary"] = {
        key: value for key, value in dev_metrics.items() if key != "records"
    }
    if report["test"]:
        printable["test_summary"] = {
            key: value for key, value in report["test"].items() if key != "records"
        }
    serialized = json.dumps(printable, ensure_ascii=False, indent=2)
    print(serialized)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["exported"]:
        print(f"Qualified ONNX model exported to {args.output}")
    else:
        print("Model not exported: development/test data did not satisfy all deployment gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
