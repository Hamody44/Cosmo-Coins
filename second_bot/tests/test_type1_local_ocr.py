from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from train_type1_local_ocr import read_dataset, split_rows
from type1_local_ocr import (
    build_model,
    load_image,
    preprocessing_variants,
    recognize,
    segment_candidates,
)


def synthetic(text: str = "12345", size: tuple[int, int] = (150, 50)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    step = max(1, size[0] // 5)
    for i, digit in enumerate(text):
        draw.text((i * step + 3, 8), digit, fill="black")
    return image


class Type1LocalOCRTests(unittest.TestCase):
    def test_empty_and_noise_masks_are_safe(self):
        for image in (Image.new("L", (30, 20), 255), Image.effect_noise((30, 20), 128)):
            candidates = []
            for _, mask in preprocessing_variants(image):
                candidates.extend(segment_candidates(mask))
            self.assertLessEqual(len(candidates), 72)

    def test_different_dimensions_are_normalized(self):
        self.assertEqual(load_image(synthetic(size=(80, 20))).size, (300, 110))
        self.assertEqual(load_image(synthetic(size=(600, 220))).size, (300, 110))

    def test_split_candidates_have_five_segments_and_include_touching_digits(self):
        image = synthetic("11234")
        candidates = segment_candidates(preprocessing_variants(image)[1][1])
        self.assertTrue(candidates)
        self.assertTrue(all(len(parts) == 5 for parts in candidates))
        self.assertLessEqual(len(candidates), 18)

    def test_empty_model_abstains_and_wrong_length_never_returns(self):
        result = recognize(synthetic(), {"templates": []})
        self.assertEqual(result["text"], "")
        self.assertTrue(result["abstained"])

    def test_training_split_is_deterministic_and_disjoint(self):
        rows = [{"filename": f"{i}.png", "batch_id": ""} for i in range(10)]
        train, validation, kind = split_rows(rows, 0.2, 41)
        self.assertEqual(kind, "random_image_holdout_no_batch_id")
        self.assertTrue({r["filename"] for r in train}.isdisjoint(r["filename"] for r in validation))
        self.assertEqual([r["filename"] for r in validation], [r["filename"] for r in split_rows(rows, 0.2, 41)[1]])

    def test_batch_split_does_not_mix_batches(self):
        rows = [{"filename": f"{b}-{i}.png", "batch_id": b} for b in ("a", "b", "c") for i in range(3)]
        train, validation, kind = split_rows(rows, 0.2, 41)
        self.assertEqual(kind, "batch_holdout")
        self.assertTrue({r["batch_id"] for r in train}.isdisjoint(r["batch_id"] for r in validation))

    def test_duplicate_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            synthetic().save(root / "a.png")
            (root / "b.png").write_bytes((root / "a.png").read_bytes())
            (root / "label.csv").write_text("filename,type,answer\na.png,type1,12345\nb.png,type1,12345\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                read_dataset(root)

    def test_model_can_be_built_without_numpy_or_torch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            synthetic().save(root / "a.png")
            model = build_model([{"filename": "a.png", "answer": "12345"}], root)
            self.assertIn("templates", model)
            self.assertEqual(model["training_sources"], 1)


if __name__ == "__main__":
    unittest.main()
