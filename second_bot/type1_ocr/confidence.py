"""Conservative confidence scoring and local-candidate consensus policies."""
from __future__ import annotations

from collections.abc import Iterable

from .types import Type1Result


def score_confidence(mean_distance: float, digit_confidence: Iterable[dict], *, max_distance: float = 0.34) -> float:
    """Map distance and digit margins to a bounded, conservative 0..1 score."""
    if max_distance <= 0 or mean_distance < 0:
        return 0.0
    distance_component = max(0.0, min(1.0, 1.0 - mean_distance / max_distance))
    digits = list(digit_confidence)
    if not digits:
        return 0.0
    # A margin near 0.08 (in this feature-distance scale) is strong evidence.
    margin_component = sum(max(0.0, min(1.0, float(item.get("margin", 0.0)) / 0.08)) for item in digits) / len(digits)
    return max(0.0, min(1.0, 0.7 * distance_component + 0.3 * margin_component))


def is_confident(result: Type1Result, *, minimum: float = 0.35) -> bool:
    return bool(
        not result.abstained
        and result.reliable
        and len(result.text) == 5
        and result.text.isascii()
        and result.text.isdigit()
        and result.confidence >= minimum
    )


def safe_consensus(results: Iterable[Type1Result], *, minimum: float = 0.55) -> Type1Result:
    """Accept only one unambiguous value supported by >=2 confident candidates.

    Conflicting high-confidence values and single-source readings abstain, so
    callers can continue to their configured provider fallback paths.
    """
    confident = [result for result in results if is_confident(result, minimum=minimum)]
    values = {result.text for result in confident}
    if len(values) != 1:
        reason = "conflicting confident candidates" if len(values) > 1 else "insufficient confident candidates"
        return Type1Result(reason=reason)
    matches = [result for result in confident if result.text in values]
    if len(matches) < 2:
        return Type1Result(reason="single candidate; use configured fallback")
    value = matches[0].text
    return Type1Result(
        text=value,
        confidence=min(result.confidence for result in matches),
        reliable=True,
        abstained=False,
        mean_distance=max((result.mean_distance or 0.0) for result in matches),
        digit_confidence=matches[0].digit_confidence,
    )
