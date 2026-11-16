from __future__ import annotations

import unittest
import numpy as np

from type1 import choose_retry_candidate, composite_distance, local_boundary_candidates


class Type3OptimizationTests(unittest.TestCase):
    def test_local_boundary_search_is_bounded_and_independent(self):
        candidates = local_boundary_candidates(40, 80, lower_bound=20, upper_bound=100, minimum_width=20, maximum_width=60)
        self.assertIn((40, 80), candidates)
        self.assertTrue(all(20 <= left < right <= 100 for left, right in candidates))
        self.assertTrue(all(20 <= right - left <= 60 for left, right in candidates))
        self.assertEqual(len(candidates), 49)

    def test_composite_distance_is_zero_for_identical_masks(self):
        mask = np.zeros((12, 9), dtype=bool)
        mask[2:10, 3:6] = True
        self.assertAlmostEqual(composite_distance(mask, mask), 0.0)

    def test_low_margin_retries_but_confident_primary_is_kept(self):
        primary = ("3", 0.30)
        self.assertEqual(choose_retry_candidate(primary, [("8", 0.10)], margin=0.03), primary)
        self.assertEqual(choose_retry_candidate(primary, [("8", 0.10)], margin=0.01), ("8", 0.10))


if __name__ == "__main__":
    unittest.main()
