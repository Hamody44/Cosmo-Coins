from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

import local_ocr
from type1_local_ocr import load_image as legacy_load_image
from type1_local_ocr import recognize as legacy_recognize
from type1_ocr import load_image, recognize_type1
from tools.evaluate_type1_rgb import channel_variants
from tools.train_type1 import read_dataset as new_read_dataset
from train_type1_local_ocr import read_dataset as old_read_dataset


class Type1CompatibilityTests(unittest.TestCase):
    def test_legacy_and_package_image_loaders_match(self):
        image = Image.new("RGB", (80, 40), "white")
        self.assertEqual(legacy_load_image(image).tobytes(), load_image(image).tobytes())

    def test_legacy_and_package_empty_model_recognition_match(self):
        image = Image.new("RGB", (300, 110), "white")
        self.assertEqual(legacy_recognize(image, {"templates": []})["text"], recognize_type1(image, {"templates": []}).text)

    def test_old_training_imports_and_new_training_imports_select_same_type1_dataset(self):
        root = Path(__file__).resolve().parents[2] / "samples"
        self.assertEqual([r["filename"] for r in old_read_dataset(root)], [r["filename"] for r in new_read_dataset(root)])

    def test_type2_module_and_model_remain_available_unchanged(self):
        model_path = Path(__file__).resolve().parents[1] / "models" / "local_ocr_model.json"
        model = local_ocr.load_model(model_path)
        type2_image = Path(__file__).resolve().parents[2] / "samples" / "type2_01.webp"
        result = local_ocr.recognize(type2_image, model)
        self.assertIn("type", result)
        self.assertEqual(result["type"], "type2")

    def test_rgb_compatibility_helper_still_exposes_channels(self):
        variants = dict(channel_variants(Image.new("RGB", (300, 110), "white")))
        self.assertIn("red", variants)
        self.assertIn("blue", variants)


if __name__ == "__main__":
    unittest.main()
