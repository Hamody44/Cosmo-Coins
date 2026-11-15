from __future__ import annotations

import math
import unittest

from PIL import Image

from type1_ocr import Type1Result, recognize_type1, safe_consensus
from type1_ocr.features import feature_distance


class Type1ApiTests(unittest.TestCase):
    def test_feature_distance_is_normalized_euclidean(self):
        self.assertTrue(math.isclose(feature_distance([0.0, 1.0], [1.0, 1.0]), math.sqrt(0.5)))

    def test_empty_template_model_abstains_with_structured_reason(self):
        result = recognize_type1(Image.new("RGB", (300, 110), "white"), {"templates": []})
        self.assertTrue(result.abstained)
        self.assertFalse(result.reliable)
        self.assertEqual(result.text, "")
        self.assertTrue(result.reason)

    def test_low_confidence_or_malformed_result_never_counts_as_consensus(self):
        weak = Type1Result(text="12345", confidence=0.1, reliable=True, abstained=False)
        self.assertTrue(safe_consensus([weak, weak]).abstained)

    def test_conflicting_confident_candidates_abstain(self):
        one = Type1Result(text="12345", confidence=0.9, reliable=True, abstained=False)
        two = Type1Result(text="12346", confidence=0.9, reliable=True, abstained=False)
        self.assertIn("conflict", safe_consensus([one, two]).reason or "")

    def test_consensus_requires_at_least_two_matching_confident_candidates(self):
        first = Type1Result(text="12345", confidence=0.9, reliable=True, abstained=False)
        second = Type1Result(text="12345", confidence=0.8, reliable=True, abstained=False)
        accepted = safe_consensus([first, second])
        self.assertEqual(accepted.text, "12345")
        self.assertTrue(accepted.reliable)


if __name__ == "__main__":
    unittest.main()
