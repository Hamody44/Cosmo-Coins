from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from type1_ocr import predict_with_confidence, recognize_type1
from type1_ocr.features_type1 import FEATURE_LENGTH, extract_features, load_labeled_crops
from type1_ocr.templates import predict_segments
from tools.train_type1 import make_classifier, split_crop_samples


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


class Type1SklearnFeatureTests(unittest.TestCase):
    def test_feature_vector_has_1155_entries_and_preserves_crop_metadata(self):
        crop = Image.new("L", (8, 20), 255)
        ImageDraw.Draw(crop).line((3, 2, 3, 17), fill=0, width=2)
        features = extract_features(crop)
        self.assertEqual(features.shape, (1155,))
        self.assertAlmostEqual(float(features[1152]), 8 / 20, places=6)
        self.assertAlmostEqual(float(features[1153]), 20 / 110, places=6)
        self.assertGreater(float(features[1154]), 0.0)

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

    def test_classifier_uses_required_standardized_rbf_svc(self):
        classifier = make_classifier()
        self.assertEqual([name for name, _ in classifier.steps], ["standardscaler", "svc"])
        params = classifier.named_steps["svc"].get_params()
        self.assertEqual(params["kernel"], "rbf")
        self.assertEqual(params["C"], 4.0)
        self.assertEqual(params["gamma"], "scale")
        self.assertTrue(params["probability"])

    def test_five_digit_probabilities_are_requested_in_one_batch(self):
        model = _BatchProbe()
        segments = [Image.new("L", (8 + index, 20), 255) for index in range(5)]
        prediction = predict_segments(segments, model)
        self.assertEqual(model.seen_shape, (5, FEATURE_LENGTH))
        self.assertEqual(prediction["text"], "77777")
        self.assertEqual(len(prediction["confidence"]), 5)

    def test_persisted_model_is_compact_and_public_api_predicts(self):
        repository = Path(__file__).resolve().parents[2]
        model_path = repository / "second_bot" / "models" / "type1_model.joblib"
        image_path = repository / "samples" / "type1_01.webp"
        self.assertLess(model_path.stat().st_size, 2_000_000)
        code, minimum, per_digit = predict_with_confidence(image_path)
        self.assertEqual(code, "12880")
        self.assertEqual(len(per_digit), 5)
        self.assertEqual(minimum, min(per_digit))
        result = recognize_type1(image_path)
        self.assertEqual(result.text, code)
        self.assertTrue(result.reliable)


if __name__ == "__main__":
    unittest.main()
