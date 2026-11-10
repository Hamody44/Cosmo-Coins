import csv
import unittest
from pathlib import Path

from local_ocr import detect_style, digit_features, load_image, load_model, predict_digit_features
from train_local_ocr import make_templates, prepare_rows, read_dataset

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "captcha_samples"
MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "local_ocr_model.json"


class LocalOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_model(MODEL_PATH)
        cls.rows = read_dataset(DATA_DIR)

    def test_user_corrected_labels_are_saved(self):
        expected = {
            "type1_19.webp": "67537",
            "type2_04.webp": "18511",
            "type2_16.webp": "77793",
        }
        with (DATA_DIR / "label.csv").open(encoding="utf-8", newline="") as handle:
            labels = {row["filename"]: row["answer"] for row in csv.DictReader(handle)}
        for filename, answer in expected.items():
            self.assertEqual(labels[filename], answer)
            model_digits = sorted(
                (template for template in self.model["templates"] if template["filename"] == filename),
                key=lambda template: template["position"],
            )
            self.assertEqual("".join(template["digit"] for template in model_digits), answer)

    def test_style_detection_for_both_styles(self):
        for filename, expected_style in (
            ("type1_01.webp", "type1"),
            ("type2_01.webp", "type2"),
        ):
            image = load_image(DATA_DIR / filename)
            self.assertEqual(detect_style(image), expected_style)

    def test_type2_leave_one_image_out_recognition(self):
        rows = [row for row in self.rows if row["type"] == "type2"]
        prepared = prepare_rows(rows, DATA_DIR)
        templates = make_templates(prepared)
        for row in prepared:
            usable = [template for template in templates if template["filename"] != row["filename"]]
            guess = predict_digit_features(row["char_features"], usable)["text"]
            self.assertEqual(guess, row["answer"], row["filename"])

    def test_type1_inference_path_runs(self):
        image = load_image(DATA_DIR / "type1_01.webp")
        result = predict_digit_features(
            digit_features(image, "type1"),
            [t for t in self.model["templates"] if t["type"] == "type1"],
        )
        self.assertEqual(len(result["text"]), 5)
        self.assertTrue(result["text"].isdigit())


if __name__ == "__main__":
    unittest.main()
