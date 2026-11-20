"""Minimal Gemini request value objects used by the optional injected-client path."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Blob:
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class Part:
    inline_data: Blob

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> "Part":
        return cls(inline_data=Blob(data=bytes(data), mime_type=mime_type))


@dataclass(frozen=True)
class GenerateContentConfig:
    temperature: float = 0.0
    max_output_tokens: int = 128
    response_mime_type: str = "application/json"
