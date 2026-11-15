"""Image loading and normalization for Type1 inputs."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ImageDecodeError
from .types import IMAGE_SIZE

ImageSource = bytes | bytearray | str | Path | Image.Image | BinaryIO


def load_image(source: ImageSource) -> Image.Image:
    """Decode an image, honor EXIF orientation, convert to RGB, and normalize size.

    Pillow's registered decoders are used, which includes PNG and WebP. The
    caller's Image instance and file handle are not closed or modified.
    """
    try:
        if isinstance(source, Image.Image):
            image = source.copy()
        elif isinstance(source, (bytes, bytearray)):
            image = Image.open(BytesIO(bytes(source)))
        else:
            image = Image.open(source)
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.size != IMAGE_SIZE:
            image = image.resize(IMAGE_SIZE, Image.Resampling.LANCZOS)
        return image
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ImageDecodeError(f"Could not decode Type1 image: {exc}") from exc
