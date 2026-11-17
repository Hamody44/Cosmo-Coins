from __future__ import annotations

import unittest
import numpy as np

from type1 import beam_decode, choose_retry_candidate, composite_distance, local_boundary_candidates, rank_position_candidates


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

    def test_beam_decode_returns_lowest_scoring_strings(self):
        candidates = [[("1", 0.1), ("7", 0.4)], [("2", 0.1), ("8", 0.2)]]
        self.assertEqual(beam_decode(candidates, beam_width=2)[0], ("12", 0.2))

    def test_rank_position_candidates_is_bounded(self):
        glyph = np.zeros((8, 8), dtype=bool)
        templates = {"0": [glyph], "1": [~glyph], "2": [glyph.copy()]}
        self.assertEqual(len(rank_position_candidates(glyph, templates, limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
