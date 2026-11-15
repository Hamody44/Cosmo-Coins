from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from type1_ocr.segmentation import propose_splits, segment_candidates


class Type1SegmentationTests(unittest.TestCase):
    def test_blank_mask_produces_no_candidates(self):
        mask = Image.new("L", (300, 110), 0)
        self.assertEqual(propose_splits(mask), [])
        self.assertEqual(segment_candidates(mask), [])

    def test_candidates_are_five_way_and_bounded(self):
        mask = Image.new("L", (300, 110), 0)
        ImageDraw.Draw(mask).text((20, 25), "11234", fill=255)
        candidates = segment_candidates(mask, max_candidates=7)
        self.assertLessEqual(len(candidates), 7)
        self.assertTrue(candidates)
        self.assertTrue(all(len(candidate) == 5 for candidate in candidates))

    def test_max_candidates_must_not_create_unbounded_work(self):
        mask = Image.new("L", (300, 110), 0)
        ImageDraw.Draw(mask).text((20, 25), "12345", fill=255)
        self.assertEqual(propose_splits(mask, max_candidates=0), [])


if __name__ == "__main__":
    unittest.main()
