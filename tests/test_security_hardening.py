"""一般公開前のAPI境界に対する攻撃回帰テスト。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


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
