from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from type1_local_ocr import (
    CELL_COUNT,
    FEATURE_SIZE,
    PREPROCESSING_VARIANTS,
    _position_class_weights,
    extract_features,
    make_masks,
    normalize_mask,
    otsu_threshold,
)


class Type1LocalOCRTests(unittest.TestCase):
    def test_position_class_weights_raise_rare_class_weight(self):
        templates = [
            {"digit": "0", "variant": variant, "filename": filename}
            for filename in ("a.webp", "b.webp")
            for variant in PREPROCESSING_VARIANTS
        ] + [
            {"digit": "1", "variant": variant, "filename": "c.webp"}
            for variant in PREPROCESSING_VARIANTS
        ]
        weights = _position_class_weights(templates, 0.25)
        self.assertGreater(weights["1"], weights["0"])
        self.assertEqual(_position_class_weights(templates, 0.0), {})

    def test_otsu_threshold_is_bounded(self):
        image = Image.new("L", (20, 20), 255)
        self.assertGreaterEqual(otsu_threshold(image), 0)
        self.assertLessEqual(otsu_threshold(image), 255)

    def test_masks_and_features_have_expected_shape(self):
        image = Image.new("RGB", (300, 110), "white")
        masks = make_masks(image)
        self.assertEqual(set(masks), set(PREPROCESSING_VARIANTS))
        normalized = normalize_mask(next(iter(masks.values())))
        self.assertEqual(normalized.size, FEATURE_SIZE)
        features = extract_features(image)
        self.assertEqual(set(features), set(PREPROCESSING_VARIANTS))
        for variant in PREPROCESSING_VARIANTS:
            self.assertEqual(len(features[variant]), CELL_COUNT)
            self.assertTrue(all(features[variant]))

    def test_real_dataset_images_can_be_featured(self):
        sample = Path(__file__).resolve().parents[2] / "captcha_samples" / "type1_01.webp"
        if not sample.is_file():
            self.skipTest("captcha_samples are not available")
        features = extract_features(sample)
        self.assertEqual(len(features["contrast"]), CELL_COUNT)


if __name__ == "__main__":
    unittest.main()
