"""Render the Soramimic face-free listening-ear symbol deterministically."""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/soramimic_video/static/logo-soramimic-symbol-v1.png"

SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#159cf0"/>
      <stop offset="1" stop-color="#2f73e8"/>
    </linearGradient>
    <clipPath id="disc">
      <circle cx="256" cy="256" r="240"/>
    </clipPath>
  </defs>
  <circle cx="256" cy="256" r="240" fill="url(#sky)"/>
  <g clip-path="url(#disc)" fill="#fff">
    <path d="
      M 199,416
      C 190,359 145,272 132,198
      C 123,148 138,119 162,124
      C 198,132 219,284 224,396
      Z"/>
    <path d="
      M 313,416
      C 322,359 367,272 380,198
      C 389,148 374,119 350,124
      C 314,132 293,284 288,396
      Z"/>
    <ellipse cx="256" cy="474" rx="86" ry="92"/>
  </g>
</svg>
"""


def render() -> None:
    """Render at 4x and downsample for clean, stable antialiasing."""
    high_resolution = cairosvg.svg2png(
        bytestring=SVG.encode("utf-8"), output_width=2048, output_height=2048
    )
    with Image.open(io.BytesIO(high_resolution)) as image:
        final = image.convert("RGBA").resize((512, 512), Image.Resampling.LANCZOS)
        final.save(OUTPUT, format="PNG", optimize=True)


if __name__ == "__main__":
    render()
