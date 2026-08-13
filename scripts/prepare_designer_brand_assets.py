"""Prepare web assets from the latest Canva-exported brand PNGs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets/brand"
STATIC = ROOT / "src/soramimic_video/static"

SOURCES = {
    "symbol": (
        BRAND / "soramimic-canva-symbol.png",
        "514514fc162a098567791efdba21a979c111d4d14ab8791090a12ab87b1a5b23",
    ),
    "wordmark": (
        BRAND / "soramimic-canva-wordmark.png",
        "bc8d66d314950f4d812841a362106057520b7d3726a0bf08a5f55379b05f38bf",
    ),
    "horizontal": (
        BRAND / "soramimic-canva-horizontal-v2.png",
        "776d30c3c63a469960cc5862b7fc661761b4bd252087c053a6f828220b800c9a",
    ),
    "video": (
        BRAND / "soramimic-canva-video-v2.png",
        "ef803d472b898d59c293fcc97c9d508c39547e197471ffc475d8249d27b1520b",
    ),
}

OUTPUTS = {
    "symbol": STATIC / "logo-soramimic-symbol-v3.png",
    "wordmark": STATIC / "logo-soramimic-wordmark-v2.png",
    "horizontal": STATIC / "logo-soramimic-horizontal-v2.png",
    "video": STATIC / "logo-soramimic-video-v2.png",
}

OGP_BASE = STATIC / "ogp-soramimic-v2.png"
OGP_OUTPUT = STATIC / "ogp-soramimic-v5.png"


def _source(name: str) -> Image.Image:
    path, expected_sha256 = SOURCES[name]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f"{name} brand master does not match the approved source")
    image = Image.open(path)
    if image.mode != "RGBA" or image.size != (2000, 2000):
        raise ValueError(
            f"unexpected {name} master: mode={image.mode}, size={image.size}"
        )
    return image


def _trim(image: Image.Image) -> Image.Image:
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("brand master is empty")
    return image.crop(bounds)


def _trim_primary_row_band(image: Image.Image) -> Image.Image:
    """Trim the main lockup while excluding detached export-canvas artwork."""
    alpha = image.getchannel("A")
    occupied_rows = [
        y for y in range(image.height) if alpha.crop((0, y, image.width, y + 1)).getbbox()
    ]
    if not occupied_rows:
        raise ValueError("brand master is empty")

    bands: list[tuple[int, int]] = []
    start = previous = occupied_rows[0]
    for y in occupied_rows[1:]:
        if y != previous + 1:
            bands.append((start, previous + 1))
            start = y
        previous = y
    bands.append((start, previous + 1))

    start, end = max(bands, key=lambda band: band[1] - band[0])
    return _trim(image.crop((0, start, image.width, end)))


def _symbol(source: Image.Image) -> Image.Image:
    trimmed = _trim(source)
    scale = min(480 / trimmed.width, 480 / trimmed.height)
    trimmed = trimmed.resize(
        (round(trimmed.width * scale), round(trimmed.height * scale)),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (512, 512))
    canvas.alpha_composite(
        trimmed,
        ((canvas.width - trimmed.width) // 2, (canvas.height - trimmed.height) // 2),
    )
    return canvas


def _restore_ogp_background(image: Image.Image, bottom: int = 305) -> None:
    """Remove the old lockup while retaining the v2 card's smooth background."""
    pixels = image.load()
    for y in range(bottom):
        left = image.getpixel((0, y))
        right = image.getpixel((image.width - 1, y))
        for x in range(image.width):
            ratio = x / (image.width - 1)
            pixels[x, y] = tuple(
                round(left[channel] * (1 - ratio) + right[channel] * ratio)
                for channel in range(3)
            )


def _ogp(lockup: Image.Image) -> Image.Image:
    image = Image.open(OGP_BASE).convert("RGB")
    if image.size != (1200, 630):
        raise ValueError(f"unexpected OGP base size: {image.size}")

    _restore_ogp_background(image)
    target_width = 880
    target_height = round(lockup.height * target_width / lockup.width)
    lockup = lockup.resize((target_width, target_height), Image.Resampling.LANCZOS)
    image.paste(lockup, ((image.width - target_width) // 2, 65), lockup)

    return image


def prepare() -> None:
    sources = {name: _source(name) for name in SOURCES}
    outputs = {
        "symbol": _symbol(sources["symbol"]),
        "wordmark": _trim(sources["wordmark"]),
        "horizontal": _trim(sources["horizontal"]),
        "video": _trim_primary_row_band(sources["video"]),
    }
    for name, image in outputs.items():
        image.save(OUTPUTS[name], format="PNG", optimize=True)
    _ogp(outputs["video"]).save(OGP_OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    prepare()
