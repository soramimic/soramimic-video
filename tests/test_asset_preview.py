from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from soramimic_video import asset_store
from soramimic_video.asset_preview import MAX_DIMENSION, derive_asset_preview


def _source(path: Path, fmt: str, mode: str = "RGB", size=(1024, 256)) -> bytes:
    color = (10, 20, 30, 77) if mode == "RGBA" else (10, 20, 30)
    image = Image.new(mode, size, color)
    kwargs = {}
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "must be removed")
        kwargs["pnginfo"] = info
        kwargs["icc_profile"] = b"test-profile"
    image.save(path, format=fmt, **kwargs)
    return path.read_bytes()


@pytest.mark.parametrize(
    ("fmt", "suffix", "mode"),
    [("PNG", ".png", "RGBA"), ("JPEG", ".jpg", "RGB"), ("WEBP", ".webp", "RGBA")],
)
def test_derives_bounded_metadata_free_png(tmp_path: Path, fmt: str, suffix: str, mode: str):
    source = tmp_path / f"source{suffix}"
    _source(source, fmt, mode)
    out = derive_asset_preview(source, tmp_path / "preview", asset_id=f"asset:{fmt}")

    assert out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (MAX_DIMENSION, 128)
        assert "comment" not in {key.lower() for key in image.info}
        assert "icc_profile" not in image.info
        assert "exif" not in image.info
        if mode == "RGBA":
            assert image.convert("RGBA").getpixel((0, 0))[3] == 77


def test_small_image_is_always_reencoded(tmp_path: Path):
    source = tmp_path / "small.png"
    original = _source(source, "PNG", "RGBA", size=(8, 8))
    out = derive_asset_preview(source, tmp_path / "preview", asset_id="small")
    response = out.read_bytes()
    assert response != original
    assert hashlib.sha256(response).digest() != hashlib.sha256(original).digest()


def test_animation_is_flattened_to_first_frame(tmp_path: Path):
    source = tmp_path / "animated.webp"
    frames = [Image.new("RGBA", (12, 8), color) for color in ("red", "blue")]
    frames[0].save(source, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    out = derive_asset_preview(source, tmp_path / "preview", asset_id="animated")
    with Image.open(out) as image:
        assert getattr(image, "n_frames", 1) == 1
        assert image.convert("RGB").getpixel((0, 0))[0] > 200


def test_source_update_changes_content_key_and_preview(tmp_path: Path):
    source = tmp_path / "source.png"
    _source(source, "PNG", size=(40, 20))
    first = derive_asset_preview(
        source, tmp_path / "preview", asset_id="same", source_revision="1"
    )
    first_bytes = first.read_bytes()
    Image.new("RGB", (40, 20), "blue").save(source)
    second = derive_asset_preview(
        source, tmp_path / "preview", asset_id="same", source_revision="2"
    )
    assert second != first
    assert second.read_bytes() != first_bytes


def test_poisoned_derived_cache_without_matching_digest_is_regenerated(tmp_path: Path):
    source = tmp_path / "source.png"
    original = _source(source, "PNG", size=(20, 10))
    cache = tmp_path / "preview"
    out = derive_asset_preview(source, cache, asset_id="same")
    out.write_bytes(original)
    out.with_suffix(".sha256").unlink()
    repaired = derive_asset_preview(source, cache, asset_id="same")
    assert repaired == out
    assert repaired.read_bytes() != original


def test_svg_uses_rasterization_then_png_derivation(tmp_path: Path):
    source = tmp_path / "source.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="300">'
        '<rect width="900" height="300" fill="red"/></svg>',
        encoding="utf-8",
    )
    out = derive_asset_preview(source, tmp_path / "preview", asset_id="svg")
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (512, 171)
    assert source.is_file()


def test_extreme_svg_aspect_ratio_is_rejected_before_rasterization(tmp_path: Path):
    source = tmp_path / "extreme.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 10000"></svg>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SVG"):
        derive_asset_preview(source, tmp_path / "preview", asset_id="extreme")


def test_excessive_raster_pixels_are_rejected(tmp_path: Path):
    source = tmp_path / "large.png"
    Image.new("1", (5000, 5000), 1).save(source)
    with pytest.raises(ValueError, match="decode"):
        derive_asset_preview(source, tmp_path / "preview", asset_id="large")


def test_excessive_source_bytes_are_rejected_before_read(tmp_path: Path):
    from soramimic_video.asset_preview import MAX_SOURCE_BYTES

    source = tmp_path / "oversized.png"
    with source.open("wb") as handle:
        handle.truncate(MAX_SOURCE_BYTES + 1)
    with pytest.raises(ValueError, match="不正"):
        derive_asset_preview(source, tmp_path / "preview", asset_id="oversized")


def test_verified_preview_asset_rejects_manifest_mismatch_and_symlink(tmp_path: Path):
    store = tmp_path / "store"
    images = store / "images"
    images.mkdir(parents=True)
    source = images / "source.png"
    _source(source, "PNG", size=(10, 10))
    url = "https://example.test/source.png"

    def write_manifest(local_path: str, digest: str) -> None:
        (store / "manifest.json").write_text(
            json.dumps(
                {
                    "assets": {
                        url: {
                            "status": "available",
                            "local_path": local_path,
                            "sha256": digest,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        asset_store._read_manifest.cache_clear()

    write_manifest("images/source.png", "0" * 64)
    assert asset_store.verified_preview_asset(url, store)[0:2] == (True, None)

    outside = tmp_path / "outside.png"
    _source(outside, "PNG", size=(10, 10))
    source.unlink()
    source.symlink_to(outside)
    write_manifest("images/source.png", hashlib.sha256(outside.read_bytes()).hexdigest())
    assert asset_store.verified_preview_asset(url, store)[0:2] == (True, None)


@pytest.mark.parametrize("local_path", ["../outside.png", "/tmp/outside.png"])
def test_verified_preview_asset_rejects_unsafe_manifest_path(tmp_path: Path, local_path: str):
    store = tmp_path / "store"
    store.mkdir()
    url = "https://example.test/source.png"
    (store / "manifest.json").write_text(
        json.dumps(
            {
                "assets": {
                    url: {
                        "status": "available",
                        "local_path": local_path,
                        "sha256": "0" * 64,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    asset_store._read_manifest.cache_clear()
    assert asset_store.verified_preview_asset(url, store)[0:2] == (True, None)


def test_configured_store_treats_missing_manifest_entry_as_managed_failure(tmp_path: Path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "manifest.json").write_text('{"assets": {}}', encoding="utf-8")
    assert asset_store.verified_preview_asset("https://example.test/missing.png", store)[0:2] == (
        True,
        None,
    )


def test_metadata_source_fixture_really_contains_metadata(tmp_path: Path):
    source = tmp_path / "source.png"
    raw = _source(source, "PNG", "RGBA", size=(8, 8))
    with Image.open(io.BytesIO(raw)) as image:
        assert image.info["Comment"] == "must be removed"
        assert image.info["icc_profile"] == b"test-profile"
