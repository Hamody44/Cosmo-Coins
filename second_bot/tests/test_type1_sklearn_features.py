from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from type1_ocr import predict_with_confidence, recognize_type1
from type1_ocr.features_type1 import FEATURE_LENGTH, extract_features, load_labeled_crops
from type1_ocr.templates import _predict_rbf_ovo, load_model, predict_segments
from tools.train_type1 import export_svc_artifact, make_classifier, split_crop_samples


class _BatchProbe:
    n_features_in_ = FEATURE_LENGTH
    classes_ = np.asarray(list("0123456789"))

    def __init__(self):
        self.seen_shape = None

    def predict_proba(self, X):
        self.seen_shape = X.shape
        result = np.full((len(X), 10), 0.01, dtype=np.float64)
        result[:, 7] = 0.91
        return result


class Type1NumpyModelTests(unittest.TestCase):
    def test_feature_vector_has_415_entries_and_preserves_crop_metadata(self):
        crop = Image.new("L", (8, 20), 255)
        ImageDraw.Draw(crop).line((3, 2, 3, 17), fill=0, width=2)
        features = extract_features(crop)
        self.assertEqual(features.shape, (FEATURE_LENGTH,))
        self.assertAlmostEqual(float(features[252]), 8 / 20, places=6)
        self.assertAlmostEqual(float(features[253]), 20 / 110, places=6)
        self.assertGreater(float(features[254]), 0.0)

    def test_crop_loader_skips_original_small_height_or_width(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for digit in "0123456789":
                (root / digit).mkdir()
            Image.new("L", (12, 19), 0).save(root / "0" / "too_short.png")
            Image.new("L", (4, 24), 0).save(root / "1" / "too_narrow.png")
            Image.new("L", (5, 20), 0).save(root / "2" / "valid_boundary.png")
            samples, skipped = load_labeled_crops(root)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["label"], "2")
            self.assertEqual(skipped["0"], 1)
            self.assertEqual(skipped["1"], 1)

    def test_grouped_stratified_split_has_no_source_overlap(self):
        samples = []
        for group_index in range(50):
            truth = "".join(str((group_index + position) % 10) for position in range(5))
            for position, label in enumerate(truth, 1):
                samples.append({"group": f"captcha-{group_index}", "label": label})
        train, validation, metadata = split_crop_samples(samples)
        train_groups = {samples[int(index)]["group"] for index in train}
        validation_groups = {samples[int(index)]["group"] for index in validation}
        self.assertFalse(train_groups & validation_groups)
        self.assertEqual(metadata["group_overlap"], 0)
        self.assertAlmostEqual(len(validation) / len(samples), 0.2, delta=0.04)

    def test_offline_trainer_uses_rbf_svc(self):
        classifier = make_classifier()
        self.assertEqual(classifier.kernel, "rbf")
        self.assertEqual(classifier.C, 7.0)
        self.assertEqual(classifier.gamma, "scale")
        self.assertFalse(classifier.probability)

    def test_five_digit_probabilities_are_requested_in_one_batch(self):
        model = _BatchProbe()
        segments = [Image.new("L", (8 + index, 20), 255) for index in range(5)]
        prediction = predict_segments(segments, model)
        self.assertEqual(model.seen_shape, (5, FEATURE_LENGTH))
        self.assertEqual(prediction["text"], "77777")
        self.assertEqual(len(prediction["confidence"]), 5)

    def test_training_export_contains_only_compact_svc_parameters(self):
        rng = np.random.default_rng(41)
        X = rng.normal(size=(30, FEATURE_LENGTH)).astype(np.float32)
        y = np.asarray([str(index % 10) for index in range(len(X))])
        classifier = make_classifier()
        classifier.fit(X, y)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.npz"
            size = export_svc_artifact(classifier, path)
            self.assertLess(size, 2_000_000)
            with np.load(path, allow_pickle=False) as archive:
                self.assertEqual(set(archive.files), {"support_vectors_", "_support_vector_scale_", "_support_vector_min_", "dual_coef_", "intercept_", "_gamma", "classes_", "n_support_"})
            model = load_model(path)
            self.assertEqual(model["model_kind"], "rbf_ovo")
            probe = rng.normal(size=(40, FEATURE_LENGTH)).astype(np.float32)
            predicted_indices, _ = _predict_rbf_ovo(probe, model)
            actual = np.asarray(model["classes_"])[predicted_indices]
            self.assertTrue(np.array_equal(actual, classifier.predict(probe).astype(str)))

    def test_package_has_no_sklearn_scipy_or_joblib_imports(self):
        package = Path(__file__).resolve().parents[1] / "type1_ocr"
        forbidden = {"sklearn", "scipy", "joblib"}
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                self.assertTrue(forbidden.isdisjoint(names), f"forbidden import in {path}: {names}")

    def test_repository_artifact_is_compact_and_public_api_predicts(self):
        repository = Path(__file__).resolve().parents[2]
        model_path = repository / "second_bot" / "models" / "type1_model.npz"
        self.assertLess(model_path.stat().st_size, 2_000_000)
        sample = repository / "samples" / "type1_01.webp"
        code, minimum, per_digit = predict_with_confidence(sample)
        self.assertEqual(len(code), 5)
        self.assertEqual(len(per_digit), 5)
        self.assertEqual(minimum, min(per_digit))
        result = recognize_type1(sample)
        self.assertEqual(result.text, code)


if __name__ == "__main__":
    unittest.main()
