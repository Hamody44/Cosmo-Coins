from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from type1_ocr import build_model
from tools.train_type1 import read_dataset


class Type1DatasetTests(unittest.TestCase):
    def test_repository_dataset_has_type1_only_and_keeps_png_webp(self):
        root = Path(__file__).resolve().parents[2] / "samples"
        rows = read_dataset(root)
        self.assertEqual(len(rows), 309)
        self.assertTrue(all(row["type"] == "type1" and not row["filename"].startswith("type2_") for row in rows))
        formats = {Path(row["filename"]).suffix.lower() for row in rows}
        self.assertTrue({".png", ".webp"}.issubset(formats))

    def test_temporary_type2_rows_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (300, 110), "white").save(root / "t1.png")
            Image.new("RGB", (300, 110), "white").save(root / "t2.png")
            with (root / "label.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["filename", "type", "answer"])
                writer.writerow(["t1.png", "type1", "12345"])
                writer.writerow(["t2.png", "type2", "54321"])
            self.assertEqual([row["filename"] for row in read_dataset(root)], ["t1.png"])

    def test_model_builder_does_not_open_type2_images(self):
        model = build_model([{"filename": "not-present.webp", "type": "type2", "answer": "12345"}], Path("."))
        self.assertEqual(model["training_sources"], 0)
        self.assertEqual(model["templates"], [])

    def test_duplicate_image_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (300, 110), "white").save(root / "a.png")
            (root / "b.png").write_bytes((root / "a.png").read_bytes())
            (root / "label.csv").write_text("filename,type,answer\na.png,type1,12345\nb.png,type1,54321\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_dataset(root)


if __name__ == "__main__":
    unittest.main()
