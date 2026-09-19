from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from soramimic_video import api as api_mod
from soramimic_video import asset_store, convert

URL = "https://example.test/source.png"


def _png(path: Path, color: str = "red", size=(900, 300)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "source metadata")
    Image.new("RGBA", size, color).save(path, pnginfo=info)
    return path.read_bytes()


def _setup(
    tmp_path: Path,
    monkeypatch,
    *,
    usage: str = "",
    store_state: str = "valid",
) -> tuple[TestClient, bytes, Path, Path]:
    wordlists = tmp_path / "wordlists"
    wordlists.mkdir()
    with (wordlists / "allowed.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["surface", "image", "image_usage"])
        writer.writeheader()
        writer.writerow({"surface": "x", "image": URL, "image_usage": usage})
    monkeypatch.setattr(convert, "WORDLISTS_DIR", wordlists)
    monkeypatch.setattr(api_mod, "load_launch_catalog", lambda: {"wordlists": ["allowed"]})

    store = tmp_path / "store"
    source = store / "images" / "source.png"
    original = _png(source)
    local_path = "images/source.png"
    digest = hashlib.sha256(original).hexdigest()
    if store_state == "missing":
        source.unlink()
    elif store_state == "mismatch":
        digest = "0" * 64
    elif store_state == "symlink":
        outside = tmp_path / "outside.png"
        _png(outside, "blue")
        source.unlink()
        source.symlink_to(outside)
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest = {
        "assets": {
            URL: {
                "status": "available",
                "local_path": local_path,
                "sha256": digest,
                "revision": 1,
            }
        }
    }
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    asset_store._read_manifest.cache_clear()
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    jobs = tmp_path / "jobs"
    return TestClient(api_mod.create_app(jobs_dir=jobs)), original, source, jobs


@pytest.mark.parametrize(
    ("public", "simple"), [(False, False), (False, True), (True, False), (True, True)]
)
def test_all_runtime_modes_return_only_derived_png(tmp_path, monkeypatch, public, simple):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1" if public else "0")
    monkeypatch.setenv(api_mod.SIMPLE_UI_ENV, "1" if simple else "0")
    client, original, _source, _jobs = _setup(tmp_path, monkeypatch)
    for route in ("/api/asset-preview", "/api/wordlist-image"):
        response = client.get(route, params={"wordlist": "allowed"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.content != original
        with Image.open(io.BytesIO(response.content)) as image:
            assert max(image.size) <= 512
            assert "Comment" not in image.info


@pytest.mark.parametrize("wordlist", ["../allowed", "/tmp/allowed.csv", "allowed.csv", ""])
def test_rejects_non_catalog_wordlist_shapes(tmp_path, monkeypatch, wordlist):
    client, _original, _source, _jobs = _setup(tmp_path, monkeypatch)
    assert client.get("/api/asset-preview", params={"wordlist": wordlist}).status_code == 404


def test_public_requires_launch_catalog_and_rejects_arbitrary_url(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    client, _original, _source, _jobs = _setup(tmp_path, monkeypatch)
    assert client.get(
        "/api/asset-preview",
        params={"wordlist": "allowed", "url": "https://attacker.test/other.png"},
    ).status_code == 404
    monkeypatch.setattr(api_mod, "load_launch_catalog", lambda: {"wordlists": []})
    assert client.get("/api/asset-preview", params={"wordlist": "allowed"}).status_code == 404


def test_simple_ui_without_public_mode_still_requires_launch_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "0")
    monkeypatch.setenv(api_mod.SIMPLE_UI_ENV, "1")
    client, _original, _source, _jobs = _setup(tmp_path, monkeypatch)
    (convert.WORDLISTS_DIR / "outside.csv").write_text(
        "surface,image\nx," + URL + "\n", encoding="utf-8"
    )
    assert client.get("/api/asset-preview", params={"wordlist": "outside"}).status_code == 404


def test_rejects_symlink_wordlist(tmp_path, monkeypatch):
    client, _original, _source, _jobs = _setup(tmp_path, monkeypatch)
    root = convert.WORDLISTS_DIR
    (root / "allowed.csv").unlink()
    outside = tmp_path / "outside.csv"
    outside.write_text("surface,image\nx," + URL + "\n", encoding="utf-8")
    (root / "allowed.csv").symlink_to(outside)
    assert client.get("/api/asset-preview", params={"wordlist": "allowed"}).status_code == 404


@pytest.mark.parametrize("store_state", ["missing", "mismatch", "symlink"])
def test_manifest_or_asset_inconsistency_fails_closed(tmp_path, monkeypatch, store_state):
    client, original, _source, jobs = _setup(tmp_path, monkeypatch, store_state=store_state)
    poison = jobs / "image-cache" / f"{hashlib.sha1(URL.encode()).hexdigest()[:16]}.png"
    poison.parent.mkdir(parents=True)
    poison.write_bytes(original)
    assert client.get("/api/asset-preview", params={"wordlist": "allowed"}).status_code == 404


def test_usage_gate_runs_before_any_asset_read(tmp_path, monkeypatch):
    client, _original, _source, _jobs = _setup(
        tmp_path, monkeypatch, usage="noncommercial_fanwork"
    )
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("asset must not be read")

    monkeypatch.setattr(asset_store, "verified_preview_asset", forbidden)
    response = client.get("/api/asset-preview", params={"wordlist": "allowed"})
    assert response.status_code == 403
    assert calls == 0


def test_legacy_original_cache_is_only_used_as_derivation_source(tmp_path, monkeypatch):
    client, original, _source, jobs = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv(asset_store.ASSET_STORE_ENV)
    old_cache = jobs / "image-cache" / f"{hashlib.sha1(URL.encode()).hexdigest()[:16]}.png"
    old_cache.parent.mkdir(parents=True)
    old_cache.write_bytes(original)
    response = client.get("/api/asset-preview", params={"wordlist": "allowed"})
    assert response.status_code == 200
    assert response.content != original
    assert (jobs / "asset-preview-cache").is_dir()


def test_preview_cache_invalidates_when_manifest_asset_changes(tmp_path, monkeypatch):
    client, _original, source, _jobs = _setup(tmp_path, monkeypatch)
    first = client.get("/api/asset-preview", params={"wordlist": "allowed"})
    assert first.status_code == 200
    changed = _png(source, "blue")
    store = source.parents[1]
    manifest = json.loads((store / "manifest.json").read_text(encoding="utf-8"))
    manifest["assets"][URL]["sha256"] = hashlib.sha256(changed).hexdigest()
    manifest["assets"][URL]["revision"] = 2
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    asset_store._read_manifest.cache_clear()
    second = client.get("/api/asset-preview", params={"wordlist": "allowed"})
    assert second.status_code == 200
    assert second.content != first.content


def test_cache_and_store_are_not_static_http_mounts(tmp_path, monkeypatch):
    client, _original, _source, _jobs = _setup(tmp_path, monkeypatch)
    assert client.get("/image-cache/source.png").status_code == 404
    assert client.get("/asset-preview-cache/source.png").status_code == 404
    assert client.get("/assets/images/source.png").status_code == 404
