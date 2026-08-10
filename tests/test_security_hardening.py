"""一般公開前のAPI境界に対する攻撃回帰テスト。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


def _fast_pipeline(job, config):
    out = job.dir / "result.mp4"
    out.write_bytes(b"video")
    return out


@pytest.fixture
def simple_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.SIMPLE_UI_ENV, "1")
    monkeypatch.setattr(api_mod, "run_pipeline", _fast_pipeline)
    monkeypatch.setattr(api_mod, "song_seconds", lambda _: 1.0)
    return TestClient(api_mod.create_app(tmp_path / "jobs"))


def _catalog_midi() -> bytes:
    return (api_mod.STATIC_DIR / "sample" / "furusato.mid").read_bytes()


def _post_job(client: TestClient, midi: bytes, name: str = "furusato.mid", **data):
    return client.post(
        "/api/jobs",
        files={"midi": (name, midi, "audio/midi")},
        data={"wordlist": "stations", **data},
    )


def test_simple_jobs_require_catalog_filename_and_sha256(simple_client: TestClient):
    midi = _catalog_midi()
    assert _post_job(simple_client, midi).status_code == 200
    assert _post_job(simple_client, midi, name="renamed.mid").status_code == 422
    tampered = midi[:-1] + bytes([midi[-1] ^ 1])
    assert _post_job(simple_client, tampered).status_code == 422


def test_simple_midi_check_uses_the_same_catalog_match(simple_client: TestClient):
    midi = _catalog_midi()
    good = simple_client.post(
        "/api/midi-check", files={"midi": ("furusato.mid", midi, "audio/midi")}
    )
    assert good.status_code == 200
    bad = simple_client.post(
        "/api/midi-check", files={"midi": ("other.mid", midi, "audio/midi")}
    )
    assert bad.status_code == 422


def test_simple_uses_bundled_lyrics_and_rejects_custom_inputs(
    simple_client: TestClient, tmp_path: Path
):
    bundled = (api_mod.STATIC_DIR / "sample" / "furusato_lyrics.txt").read_text(
        encoding="utf-8"
    )
    assert _post_job(simple_client, _catalog_midi(), lyrics=bundled).status_code == 200
    # Simple UIの隠し入力に古い値や改行差が残っていても、
    # 照合済みMIDIに付属する歌詞へサーバー側で一意に戻す。
    normalized = _post_job(simple_client, _catalog_midi(), lyrics="任意の歌詞")
    assert normalized.status_code == 200
    saved = tmp_path / "jobs" / normalized.json()["id"] / "lyrics.txt"
    assert saved.read_text(encoding="utf-8") == bundled
    custom = simple_client.post(
        "/api/jobs",
        files={
            "midi": ("furusato.mid", _catalog_midi(), "audio/midi"),
            "wordlist_csv": ("mine.csv", b"id,surface\n1,test\n", "text/csv"),
        },
        data={"wordlist": "stations"},
    )
    assert custom.status_code == 422
    editor = simple_client.post(
        "/api/jobs",
        files={
            "midi": ("furusato.mid", _catalog_midi(), "audio/midi"),
            "editor": ("editor.json", b"{}", "application/json"),
        },
        data={"wordlist": "stations"},
    )
    assert editor.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/wordlist-columns?wordlist=youtuber",
        "/api/wordlist-columns?wordlist=plant",
        "/api/wordlist-image?wordlist=football",
        "/api/wordlist-image?wordlist=plant",
        "/api/thumbnail-preview?sample=furusato&wordlist=plant",
        "/editor/wordlists/youtuber.csv",
        "/editor/wordlists/plant.csv",
    ],
)
def test_simple_get_routes_share_wordlist_allowlist(simple_client: TestClient, path: str):
    assert simple_client.get(path).status_code == 404


def test_simple_rejects_filesystem_csv_before_resolution(
    simple_client: TestClient, tmp_path: Path
):
    csv_path = tmp_path / "secret.csv"
    csv_path.write_text("id,surface,image\n1,x,/etc/passwd\n", encoding="utf-8")
    assert (
        simple_client.get("/api/wordlist-columns", params={"wordlist": str(csv_path)}).status_code
        == 404
    )
    assert (
        simple_client.get("/api/wordlist-image", params={"wordlist": str(csv_path)}).status_code
        == 404
    )


def test_simple_preview_job_cannot_bypass_wordlist_allowlist(simple_client: TestClient):
    res = _post_job(simple_client, _catalog_midi(), wordlist="football", preview="10")
    assert res.status_code == 422


def test_simple_initial_catalog_allows_baseball_with_player_layout(
    simple_client: TestClient
):
    res = _post_job(simple_client, _catalog_midi(), wordlist="baseball")
    assert res.status_code == 200
    job_id = res.json()["id"]
    for _ in range(100):
        body = simple_client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "error", "canceled"):
            break
        time.sleep(0.01)
    assert body["params"]["wordlist"] == "baseball"
    assert body["params"]["layout"] == "player_card"


def test_simple_editor_processing_endpoints_are_absent(simple_client: TestClient):
    assert simple_client.post("/api/editor-preview").status_code in (404, 422)
    assert simple_client.post("/api/editor-session").status_code in (404, 422)
    assert simple_client.post("/api/wordlist-check").status_code in (404, 422)


@pytest.mark.parametrize(
    "path",
    [
        "/editor//wordlists/football.csv",
        "/editor/wordlists//football.csv",
        "/editor/editor.html",
        "/editor/kuromoji/dict/base.dat.gz",
    ],
)
def test_simple_editor_static_mount_cannot_bypass_allowlist(
    simple_client: TestClient, path: str
):
    assert simple_client.get(path).status_code == 404


def test_simple_rejects_oversized_request_before_midi_hash(simple_client: TestClient):
    oversized = b"MThd" + b"x" * api_mod.DEFAULT_SIMPLE_MAX_REQUEST_BYTES
    res = simple_client.post(
        "/api/midi-check",
        files={"midi": ("furusato.mid", oversized, "audio/midi")},
    )
    assert res.status_code == 413


def test_operational_endpoints_are_hidden_from_proxy_users(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.OPS_TOKEN_ENV, "ops-secret")
    monkeypatch.setenv(api_mod.TRUSTED_PROXY_IPS_ENV, "127.0.0.1/32")
    app = api_mod.create_app(tmp_path / "jobs")
    client = TestClient(app, client=("127.0.0.1", 50000))
    proxy_headers = {"CF-Connecting-IP": "203.0.113.8"}

    health = client.get("/healthz", headers=proxy_headers)
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert api_mod.SESSION_COOKIE not in health.cookies
    for path in ("/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=proxy_headers).status_code == 404
        assert client.get(
            path,
            headers={**proxy_headers, "X-Soramimic-Ops-Token": "ops-secret"},
        ).status_code in (200, 503)


def test_operational_endpoints_allow_direct_localhost(tmp_path: Path):
    client = TestClient(
        api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 50000)
    )
    assert client.get("/metrics").status_code == 200
    assert client.get("/docs").status_code == 200


def test_public_mode_does_not_mistake_tunnel_loopback_for_local_ops(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    client = TestClient(
        api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 50000)
    )
    assert client.get("/metrics").status_code == 404


def test_public_job_error_does_not_expose_exception_or_log(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")

    def fail(job, config):
        raise RuntimeError("/internal/home/secret.env")

    monkeypatch.setattr(api_mod, "run_pipeline", fail)
    monkeypatch.setattr(api_mod, "song_seconds", lambda _: 1.0)
    client = TestClient(api_mod.create_app(tmp_path / "jobs"))
    res = client.post(
        "/api/jobs",
        files={"midi": ("song.mid", b"MThd" + b"\0" * 16, "audio/midi")},
        data={"wordlist": "stations"},
    )
    job_id = res.json()["id"]
    for _ in range(100):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] == "error":
            break
        time.sleep(0.01)
    assert body["error"] == "生成に失敗しました"
    assert "log" not in body
    assert "/internal/" not in str(body)
