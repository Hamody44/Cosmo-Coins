"""Domain exceptions for the standalone Type1 OCR package."""


class Type1OCRError(Exception):
    """Base exception for recoverable Type1 OCR failures."""


class ImageDecodeError(Type1OCRError, ValueError):
    """Raised when an input cannot be decoded as an image."""


class ModelFormatError(Type1OCRError, ValueError):
    """Raised when a local template model is malformed or unsupported."""
