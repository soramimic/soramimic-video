"""UI/editor向けasset previewの安全な派生と専用cache。"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import threading
import time
import warnings
from pathlib import Path

from PIL import Image

MAX_DIMENSION = 512
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_PIXELS = 20_000_000
TRANSFORM_VERSION = "asset-preview-v1"
CACHE_DIRNAME = "asset-preview-cache"
CACHE_MAX_FILES = 2000
CACHE_TTL_SEC = 30 * 24 * 3600
CACHE_PRUNE_GRACE_SEC = 10 * 60
_SVG_NUMBER = rb"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_SVG_VIEWBOX = re.compile(
    rb"\bviewBox\s*=\s*['\"]\s*(" + _SVG_NUMBER + rb")\s+(" + _SVG_NUMBER
    + rb")\s+(" + _SVG_NUMBER + rb")\s+(" + _SVG_NUMBER + rb")\s*['\"]",
    re.IGNORECASE,
)


def _svg_length_pattern(name: bytes) -> re.Pattern[bytes]:
    return re.compile(
        rb"(?:^|[\s<])" + name + rb"\s*=\s*['\"]\s*(" + _SVG_NUMBER + rb")",
        re.IGNORECASE,
    )


def preview_cache_dir(jobs_dir: Path) -> Path:
    return jobs_dir.resolve() / CACHE_DIRNAME


def _cache_key(
    asset_id: str, source_sha256: str, source_revision: str, max_dimension: int
) -> str:
    spec = {
        "asset": asset_id,
        "source_sha256": source_sha256,
        "source_revision": source_revision,
        "transform": TRANSFORM_VERSION,
        "max_dimension": max_dimension,
    }
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _svg_raster_width(source: bytes, max_dimension: int) -> int:
    head = source[:16384]
    match = _SVG_VIEWBOX.search(head)
    if match:
        width, height = float(match.group(3)), float(match.group(4))
    else:
        width_match = _svg_length_pattern(b"width").search(head)
        height_match = _svg_length_pattern(b"height").search(head)
        if not width_match or not height_match:
            raise ValueError("SVGの寸法が読めません")
        width, height = float(width_match.group(1)), float(height_match.group(1))
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise ValueError("SVGの寸法が不正です")
    output_width = max_dimension if width >= height else round(max_dimension * width / height)
    if output_width < 1:
        raise ValueError("SVGの縦横比が極端です")
    return output_width


def _derived_png(source: bytes, max_dimension: int) -> bytes:
    from .video import looks_like_svg, svg_to_png

    raster = (
        svg_to_png(source, width=_svg_raster_width(source, max_dimension))
        if looks_like_svg(source)
        else source
    )
    if raster is None:
        raise ValueError("SVGを画像に変換できません")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raster)) as opened:
                if opened.width * opened.height > MAX_SOURCE_PIXELS:
                    raise ValueError("画像のpixel数が上限を超えています")
                opened.seek(0)  # animationは先頭frameだけを静止画として使う
                pixels = opened.convert("RGBA")
                pixels.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                # sourceのinfo/exif/icc等を引き継がない新規canvasへpixelだけをコピーする。
                clean = Image.new("RGBA", pixels.size)
                clean.paste(pixels)
    except Exception as exc:
        raise ValueError("画像をdecodeできません") from exc
    out = io.BytesIO()
    clean.save(out, format="PNG", compress_level=9)
    return out.getvalue()


def _cache_hit(path: Path, digest_path: Path, max_dimension: int) -> bool:
    if not path.is_file() or path.is_symlink() or not digest_path.is_file():
        return False
    try:
        raw = path.read_bytes()
        if not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), digest_path.read_text().strip()
        ):
            return False
        with Image.open(io.BytesIO(raw)) as image:
            return image.format == "PNG" and max(image.size) <= max_dimension
    except (OSError, ValueError):
        return False


def _prune_cache(cache_dir: Path, keep: Path) -> None:
    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for path in cache_dir.glob("*.png"):
        if path == keep or path.is_symlink():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime > CACHE_TTL_SEC:
            path.unlink(missing_ok=True)
            path.with_suffix(".sha256").unlink(missing_ok=True)
        else:
            if now - mtime > CACHE_PRUNE_GRACE_SEC:
                candidates.append((mtime, path))
    candidates.sort(reverse=True)
    for _mtime, path in candidates[max(0, CACHE_MAX_FILES - 1) :]:
        path.unlink(missing_ok=True)
        path.with_suffix(".sha256").unlink(missing_ok=True)


def derive_asset_preview(
    source: Path,
    cache_dir: Path,
    *,
    asset_id: str,
    source_revision: str = "",
    expected_sha256: str = "",
    max_dimension: int = MAX_DIMENSION,
) -> Path:
    """原本を必ずdecode・PNG再encodeし、内容連動keyの専用cacheへ保存する。"""
    if max_dimension < 1 or max_dimension > MAX_DIMENSION:
        raise ValueError("previewの最大寸法が不正です")
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > MAX_SOURCE_BYTES
    ):
        raise ValueError("preview元画像が不正です")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValueError("preview元画像を読めません") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and not hmac.compare_digest(digest, expected_sha256.lower()):
        raise ValueError("preview元画像のhashが一致しません")
    key = _cache_key(asset_id, digest, source_revision, max_dimension)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{key}.png"
    digest_out = out.with_suffix(".sha256")
    if _cache_hit(out, digest_out, max_dimension):
        return out
    encoded = _derived_png(raw, max_dimension)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    digest_tmp = digest_out.with_name(
        f".{digest_out.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_bytes(encoded)
        os.replace(tmp, out)
        digest_tmp.write_text(hashlib.sha256(encoded).hexdigest(), encoding="ascii")
        os.replace(digest_tmp, digest_out)
    finally:
        tmp.unlink(missing_ok=True)
        digest_tmp.unlink(missing_ok=True)
    _prune_cache(cache_dir, out)
    return out
