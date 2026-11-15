from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from PIL import Image

from type1_local_ocr import ctc_greedy_decode, load_image, recognize


class Type1LocalOCRTests(unittest.TestCase):
    def test_load_image_keeps_fixed_canvas_without_stretching_source_aspect(self):
        source = Image.new("RGB", (200, 100), "white")
        for x in range(40, 60):
            for y in range(10, 90):
                source.putpixel((x, y), (0, 0, 0))

        image = load_image(source)
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.size, (320, 112))
        box = image.point(lambda pixel: 255 if pixel < 16 else 0).getbbox()
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        glyph_ratio = (right - left) / (bottom - top)
        self.assertGreater(glyph_ratio, 0.20)
        self.assertLess(glyph_ratio, 0.30)

    def test_ctc_decoder_collapses_runs_but_keeps_repeated_digits_after_blank(self):
        path = [0, 2, 2, 0, 2, 3, 3, 0, 4, 5, 0]
        logits = []
        for selected in path:
            row = [-8.0] * 11
            row[selected] = 8.0
            logits.append(row)

        result = ctc_greedy_decode(logits)

        self.assertEqual(result["text"], "11234")
        self.assertEqual(len(result["digit_confidences"]), 5)
        self.assertGreater(result["mean_confidence"], 0.99)

    def test_ctc_decoder_accepts_singleton_batch_output(self):
        path = [0, 2, 0, 3, 0, 4, 0, 5, 0, 6]
        logits = []
        for selected in path:
            row = [-6.0] * 11
            row[selected] = 6.0
            logits.append(row)

        self.assertEqual(ctc_greedy_decode([logits])["text"], "12345")

    def test_ctc_decoder_rejects_multiple_images_in_one_call(self):
        with self.assertRaisesRegex(ValueError, "one image"):
            ctc_greedy_decode([[[0.0] * 11], [[0.0] * 11]])

    def test_missing_model_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "not-installed.onnx"
            with self.assertRaisesRegex(FileNotFoundError, "not installed"):
                recognize(Image.new("L", (300, 110), "white"), model_path=model_path)


if __name__ == "__main__":
    unittest.main()
