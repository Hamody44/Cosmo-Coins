"""Deterministic Pillow-only preprocessing for Type1 images."""
from __future__ import annotations

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .image import load_image


def grayscale(image: Image.Image) -> Image.Image:
    """Return the legacy mild-contrast grayscale view."""
    gray = ImageOps.grayscale(load_image(image))
    return ImageEnhance.Contrast(gray).enhance(1.35)


def preprocessing_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """Return three fixed dark-ink masks and one local-background mask."""
    gray = grayscale(image)
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=7))
    variants: list[tuple[str, Image.Image]] = []
    for name, threshold in (("global_105", 105), ("global_135", 135), ("global_165", 165)):
        variants.append((name, gray.point(lambda p, t=threshold: 255 if p < t else 0)))
    local = Image.new("L", gray.size, 0)
    gp, bp, lp = gray.load(), blurred.load(), local.load()
    for y in range(gray.height):
        for x in range(gray.width):
            lp[x, y] = 255 if gp[x, y] < bp[x, y] - 18 else 0
    variants.append(("background_delta", local))
    return variants
