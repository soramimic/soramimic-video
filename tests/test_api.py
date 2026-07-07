"""APIサーバー(api.py)のテスト。

パイプライン本体はモックし、ジョブの受付→実行→動画取得の流れと
APIキー認証を確認する。NEUTRINO実行込みのE2Eは手動(serve)で行う。
"""

from __future__ import annotations

import time

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        job.stages.append({"name": "synthesize", "seconds": 0.0})
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    app = api_mod.create_app(jobs_dir=tmp_path / "jobs")
    return TestClient(app)


def wait_done(client: TestClient, job_id: str, **kw) -> dict:
    for _ in range(200):
        res = client.get(f"/api/jobs/{job_id}", **kw)
        assert res.status_code == 200
        body = res.json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("ジョブが終わりません")


def submit(client: TestClient, **fields) -> str:
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    if "editor" in fields:
        files["editor"] = ("editor.json", fields.pop("editor"), "application/json")
    res = client.post("/api/jobs", files=files, data=fields)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_job_flow_with_editor(client):
    job_id = submit(client, editor=b'{"format": "soramimic-editor/1"}')
    body = wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["params"]["parody_source"] == "editor"
    res = client.get(body["video_url"])
    assert res.status_code == 200
    assert res.content == FAKE_MP4


def test_requires_editor_or_wordlist(client):
    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    res = client.post("/api/jobs", files=files)
    assert res.status_code == 422

    job_id = submit(client, wordlist="stations")
    assert wait_done(client, job_id)["params"]["parody_source"] == "convert"


def test_preview_returns_audio(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        assert job.params["preview"] == 20.0
        out = job.dir / "neutrino" / "vocal.wav"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"RIFF-fake")
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))
    files = {
        "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
        "editor": ("editor.json", b"{}", "application/json"),
    }
    res = client.post("/api/jobs", files=files, data={"preview": "20"})
    assert res.status_code == 200
    body = wait_done(client, res.json()["id"])
    assert body["result_kind"] == "audio"
    video = client.get(body["video_url"])
    assert video.headers["content-type"] == "audio/wav"


def test_truncate_project():
    from types import SimpleNamespace

    notes = [
        SimpleNamespace(id=i, start_sec=float(i)) for i in range(5)
    ]
    lines = [
        SimpleNamespace(note_ids=[0, 1]),
        SimpleNamespace(note_ids=[2, 3]),
        SimpleNamespace(note_ids=[4]),
    ]
    project = SimpleNamespace(notes=notes, lines=lines)
    api_mod._truncate_project(project, 3.0)
    assert [n.id for n in project.notes] == [0, 1, 2]
    assert [ln.note_ids for ln in project.lines] == [[0, 1], [2]]


def test_rejects_non_midi(client):
    res = client.post("/api/jobs", files={"midi": ("x.mid", b"not midi", "audio/midi")})
    assert res.status_code == 400


def test_video_not_ready(client, monkeypatch):
    # 実行前に取りに来たら409
    slow = api_mod.run_pipeline

    def slow_pipeline(job, config):
        time.sleep(0.3)
        return slow(job, config)

    monkeypatch.setattr(api_mod, "run_pipeline", slow_pipeline)
    job_id = submit(client, editor=b"{}")
    res = client.get(f"/api/jobs/{job_id}/video")
    assert res.status_code == 409
    wait_done(client, job_id)


def test_api_key_auth(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    monkeypatch.setenv(api_mod.API_KEY_ENV, "secret-key")
    client = TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))

    files = {"midi": ("song.mid", FAKE_MIDI, "audio/midi")}
    assert client.post("/api/jobs", files=files).status_code == 401
    assert client.get("/api/jobs").status_code == 401
    # configは鍵なしでも auth_required だけ返す
    assert client.get("/api/config").json() == {"auth_required": True}

    headers = {"X-API-Key": "secret-key"}
    res = client.post(
        "/api/jobs", files=files, data={"editor": ""}, headers=headers
    )
    assert res.status_code == 422  # 認証は通り、入力バリデーションで弾かれる

    files["editor"] = ("editor.json", b"{}", "application/json")
    res = client.post("/api/jobs", files=files, headers=headers)
    assert res.status_code == 200
    job_id = res.json()["id"]
    body = wait_done(client, job_id, headers=headers)
    assert body["status"] == "done"
    # <video>タグ用にクエリパラメータでも通る
    assert client.get(f"/api/jobs/{job_id}/video?api_key=secret-key").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/video?api_key=wrong").status_code == 401


def test_restart_recovers_history(tmp_path, monkeypatch):
    def fake_pipeline(job, config):
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    jobs_dir = tmp_path / "jobs"
    client = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    job_id = submit(client, editor=b"{}")
    wait_done(client, job_id)

    client2 = TestClient(api_mod.create_app(jobs_dir=jobs_dir))
    jobs = client2.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["status"] == "done"
    assert client2.get(f"/api/jobs/{job_id}/video").content == FAKE_MP4
