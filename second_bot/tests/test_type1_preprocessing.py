from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from type1_ocr.preprocessing import preprocessing_variants


class Type1PreprocessingTests(unittest.TestCase):
    def test_returns_expected_deterministic_masks(self):
        image = Image.new("RGB", (300, 110), "white")
        ImageDraw.Draw(image).text((20, 20), "12345", fill="black")
        first = preprocessing_variants(image)
        second = preprocessing_variants(image)
        self.assertEqual([name for name, _ in first], ["global_105", "global_135", "global_165", "background_delta"])
        self.assertEqual([mask.tobytes() for _, mask in first], [mask.tobytes() for _, mask in second])
        self.assertTrue(all(mask.mode == "L" and mask.size == (300, 110) for _, mask in first))

    def test_blank_image_has_no_ink(self):
        for _, mask in preprocessing_variants(Image.new("RGB", (300, 110), "white")):
            self.assertIsNone(mask.getbbox())


if __name__ == "__main__":
    unittest.main()
