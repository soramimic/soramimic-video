from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path

import pytest
from PIL import Image

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video.asset_store import PRIVATE_ASSET_STORE_ENV  # noqa: E402
from soramimic_video.private_assets import import_private_assets  # noqa: E402
from soramimic_video.private_wordlists import (  # noqa: E402
    PRIVATE_WORDLIST_MANIFEST_ENV,
)

ASSET_ID = "asset://private/fanwork/example-character"
TERMS = "https://rights.example/terms/"


def _private_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.png"
    Image.new("RGBA", (8, 8), (20, 120, 220, 255)).save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    input_manifest = tmp_path / "private-assets-input.json"
    input_manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": ASSET_ID,
                        "source_file": str(source),
                        "source_url": "https://assets.rights.example/example.png",
                        "source_page": "https://rights.example/works/example/",
                        "sha256": digest,
                        "credit": "official artwork © Example Creator",
                        "usage": "noncommercial_fanwork",
                        "terms_page": TERMS,
                        "acquired_at": "2026-08-17",
                        "terms_reviewed_at": "2026-08-17",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = tmp_path / "private-store"
    import_private_assets(input_manifest, store)

    wordlist_root = tmp_path / "private-wordlists"
    wordlist_root.mkdir()
    csv_path = wordlist_root / "fanwork_private.csv"
    fields = [
        "id",
        "original",
        "surface",
        "pronunciation",
        "type",
        "image",
        "image_page",
        "image_credit",
        "image_usage",
        "image_terms_page",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "id": "1",
                "original": "テストタレント",
                "surface": "テストタレント",
                "pronunciation": "テストタレント",
                "type": "full",
                "image": ASSET_ID,
                "image_page": "https://rights.example/works/example/",
                "image_credit": "official artwork © Example Creator",
                "image_usage": "noncommercial_fanwork",
                "image_terms_page": TERMS,
            }
        )
    manifest = wordlist_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "wordlists": [
                    {
                        "name": "fanwork_private",
                        "label": "非公開ファン素材",
                        "phrase": "非公開素材名",
                        "layout": "youtuber_card",
                        "csv": csv_path.name,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return store, manifest


def _client(tmp_path: Path, monkeypatch, *, public: bool) -> TestClient:
    store, manifest = _private_fixture(tmp_path)
    monkeypatch.setenv(PRIVATE_ASSET_STORE_ENV, str(store))
    monkeypatch.setenv(PRIVATE_WORDLIST_MANIFEST_ENV, str(manifest))
    monkeypatch.delenv(api_mod.API_KEY_ENV, raising=False)
    if public:
        monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    else:
        monkeypatch.delenv(api_mod.PUBLIC_ENV, raising=False)
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def _fast_pipeline(job, _config):
    output = job.dir / "result.mp4"
    output.write_bytes(b"video")
    return output


def test_private_wordlist_and_image_require_explicit_ack(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_mod, "run_pipeline", _fast_pipeline)
    client = _client(tmp_path, monkeypatch, public=False)

    csv_response = client.get("/editor/wordlists/fanwork_private.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["cache-control"] == "private, no-store"
    assert ASSET_ID in csv_response.text

    denied = client.get(
        "/api/wordlist-image",
        params={"wordlist": "fanwork_private", "url": ASSET_ID},
    )
    assert denied.status_code == 403

    thumbnail_denied = client.get(
        "/api/thumbnail-preview",
        params={"sample": "furusato", "wordlist": "fanwork_private"},
    )
    assert thumbnail_denied.status_code == 403

    allowed = client.get(
        "/api/wordlist-image",
        params={
            "wordlist": "fanwork_private",
            "url": ASSET_ID,
            "noncommercial_fanwork": "true",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "private, no-store"
    assert allowed.headers["content-type"].startswith("image/png")

    setting = client.get("/editor/conf/setting.json")
    assert setting.status_code == 200
    assert "fanwork_private" in setting.text
    assert setting.headers["cache-control"] == "no-store"

    job_denied = client.post(
        "/api/jobs",
        data={"sample_id": "furusato", "wordlist": "fanwork_private"},
    )
    assert job_denied.status_code == 422
    job_allowed = client.post(
        "/api/jobs",
        data={
            "sample_id": "furusato",
            "wordlist": "fanwork_private",
            "allow_noncommercial_fanwork": "true",
        },
    )
    assert job_allowed.status_code == 200
    for _ in range(100):
        status = client.get(f"/api/jobs/{job_allowed.json()['id']}").json()
        if status["status"] in {"done", "error"}:
            break
        time.sleep(0.01)
    assert status["status"] == "done"
    assert status["params"]["wordlist"] == "fanwork_private"
    assert status["params"]["allow_noncommercial_fanwork"] is True


def test_public_runtime_ignores_leaked_private_configuration(
    tmp_path: Path, monkeypatch
):
    client = _client(tmp_path, monkeypatch, public=True)

    assert client.get("/editor/wordlists/fanwork_private.csv").status_code == 404
    response = client.get(
        "/api/wordlist-image",
        params={
            "wordlist": "fanwork_private",
            "url": ASSET_ID,
            "noncommercial_fanwork": "true",
        },
    )
    assert response.status_code == 404
    setting = client.get("/editor/conf/setting.json")
    assert setting.status_code == 200
    assert "fanwork_private" not in setting.text


def test_public_runtime_rejects_an_absolute_private_csv_path(
    tmp_path: Path, monkeypatch
):
    _store, manifest = _private_fixture(tmp_path)
    private_csv = manifest.parent / "fanwork_private.csv"
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    response = client.get(
        "/api/wordlist-image",
        params={"wordlist": str(private_csv), "url": ASSET_ID},
    )
    assert response.status_code == 404
    job = client.post(
        "/api/jobs",
        data={"sample_id": "furusato", "wordlist": str(private_csv)},
    )
    assert job.status_code == 422
