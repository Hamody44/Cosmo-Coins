"""Typed public values used by the Type1 OCR API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

CELL_COUNT = 5
IMAGE_SIZE = (300, 110)
FEATURE_SIZE = (24, 40)


@dataclass(frozen=True)
class Type1Result:
    """A five-digit recognition candidate and its explicit trust status."""

    text: str = ""
    confidence: float = 0.0
    reliable: bool = False
    abstained: bool = True
    mean_distance: float | None = None
    digit_confidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reason: str | None = None
    type: str = "type1"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping for legacy callers."""
        result = asdict(self)
        result["digit_confidence"] = list(self.digit_confidence)
        return result
