from __future__ import annotations

from io import BytesIO
import unittest

from PIL import Image

from type1_ocr import ImageDecodeError, load_image


class Type1ImageTests(unittest.TestCase):
    def _encoded(self, fmt: str) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (300, 110), "navy").save(buffer, format=fmt)
        return buffer.getvalue()

    def test_png_and_webp_are_supported(self):
        for fmt in ("PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                self.assertEqual(load_image(self._encoded(fmt)).size, (300, 110))

    def test_images_are_normalized_to_rgb_and_expected_dimensions(self):
        result = load_image(Image.new("L", (120, 44), 127))
        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.size, (300, 110))

    def test_undecodable_input_raises_package_error(self):
        with self.assertRaises(ImageDecodeError):
            load_image(b"not an image")


if __name__ == "__main__":
    unittest.main()
