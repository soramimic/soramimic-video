"""Prepare transparent web assets from the designer-provided master PNG."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/brand/soramimic-designer-horizontal.png"
SOURCE_SHA256 = "9744c339c6685c7ee5c8defe9a7b76001f645af319d678d55a879bf39dee51ec"
STATIC = ROOT / "src/soramimic_video/static"
SYMBOL_OUTPUT = STATIC / "logo-soramimic-symbol-v2.png"
WORDMARK_OUTPUT = STATIC / "logo-soramimic-wordmark-v1.png"
OGP_BASE = STATIC / "ogp-soramimic-v2.png"
OGP_OUTPUT = STATIC / "ogp-soramimic-v3.png"

# Stable coordinates in the 2000x2000 designer master (5.png in the supplied ZIP).
SYMBOL_BOX = (76, 816, 461, 1184)
WORDMARK_BOX = (507, 816, 1904, 1184)
BRAND_BLUE = (0, 153, 255)
PRODUCT_GRAY = (100, 116, 139)


def _source() -> Image.Image:
    raw = SOURCE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("designer brand master does not match the approved source")
    image = Image.open(SOURCE).convert("RGB")
    if image.size != (2000, 2000):
        raise ValueError(f"unexpected designer master size: {image.size}")
    return image


def _blue_alpha(pixel: tuple[int, int, int]) -> int:
    """Recover opacity from the blue artwork composited on a white canvas."""
    return 255 - pixel[0]


def _wordmark(source: Image.Image) -> Image.Image:
    crop = source.crop(WORDMARK_BOX)
    result = Image.new("RGBA", crop.size)
    for y in range(crop.height):
        for x in range(crop.width):
            alpha = _blue_alpha(crop.getpixel((x, y)))
            result.putpixel((x, y), (*BRAND_BLUE, alpha))
    bounds = result.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("designer wordmark is empty")
    return result.crop(bounds)


def _symbol(source: Image.Image) -> Image.Image:
    crop = source.crop(SYMBOL_BOX)
    result = Image.new("RGBA", crop.size)
    for y in range(crop.height):
        alphas = [_blue_alpha(crop.getpixel((x, y))) for x in range(crop.width)]
        colored = [x for x, alpha in enumerate(alphas) if alpha > 2]
        if not colored:
            continue
        left, right = colored[0], colored[-1]
        for x in range(left, right + 1):
            alpha = alphas[x]
            if alpha > 2:
                result.putpixel((x, y), (*BRAND_BLUE, alpha))
            else:
                # White negative space belongs to the ear/head silhouette, not the
                # removed canvas background. The outer blue contour encloses it.
                result.putpixel((x, y), (255, 255, 255, 255))
    bounds = result.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("designer symbol is empty")
    trimmed = result.crop(bounds)
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


def _ogp(symbol: Image.Image, wordmark: Image.Image) -> Image.Image:
    """Replace only the v2 brand lockup while preserving its approved copy/layout."""
    image = Image.open(OGP_BASE).convert("RGB")
    if image.size != (1200, 630):
        raise ValueError(f"unexpected OGP base size: {image.size}")
    background = image.getpixel((0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((160, 55, 1040, 295), fill=background)

    symbol = symbol.resize((180, 180), Image.Resampling.LANCZOS)
    image.paste(symbol, (170, 70), symbol)
    wordmark = wordmark.resize((590, 84), Image.Resampling.LANCZOS)
    image.paste(wordmark, (370, 106), wordmark)

    product_font = ImageFont.load_default(size=36)
    draw.text((845, 190), "video", font=product_font, fill=PRODUCT_GRAY)
    draw.rounded_rectangle((557, 263, 644, 271), radius=4, fill=BRAND_BLUE)
    return image


def prepare() -> None:
    source = _source()
    symbol = _symbol(source)
    wordmark = _wordmark(source)
    symbol.save(SYMBOL_OUTPUT, format="PNG", optimize=True)
    wordmark.save(WORDMARK_OUTPUT, format="PNG", optimize=True)
    _ogp(symbol, wordmark).save(OGP_OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    prepare()
