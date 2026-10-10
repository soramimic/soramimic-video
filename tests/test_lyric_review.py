"""Review pauses synthesis, preserves analysis, and resumes only the owning job."""

from __future__ import annotations

import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from helpers import build_xf_midi
from soramimic_video import api, mix, score_audio, synthesize, video, xfparse
from soramimic_video.lyric_review import MAX_REVIEW_BYTES, REVIEW_FILENAME
from soramimic_video.project import Project


@pytest.fixture
def review_client(tmp_path, monkeypatch):
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64)],
        lyric_events=[(0, "し"), (240, "ず"), (480, "む")],
    ).read_bytes()
    editor = tmp_path / "editor"
    editor.mkdir()
    (editor / "editor.html").write_text("<html></html>")
    calls = {"analyze": 0, "synthesize": []}
    analyze_midi = xfparse.analyze_midi

    def analyze(path):
        calls["analyze"] += 1
        return analyze_midi(path)

    def analyze_audio(_path, directory, **_kwargs):
        path = directory / "fixture.mid"
        path.write_bytes(midi)
        project = analyze(path)
        project.lines[0].original_text = "沈む"
        return project

    def sing(project, directory, **_kwargs):
        calls["synthesize"].append(copy.deepcopy(project))
        path = directory / "voice.wav"
        path.write_bytes(b"voice")
        return path

    def render(_project, directory, **_kwargs):
        path = directory / "video.mp4"
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(xfparse, "analyze_midi", analyze)
    monkeypatch.setattr(score_audio, "analyze_audio", analyze_audio)
    monkeypatch.setattr(synthesize, "synthesize", sing)
    monkeypatch.setattr(mix, "mix", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video, "make_video", render)
    app = api.create_app(jobs_dir=tmp_path / "jobs", editor_dist=editor)
    app.state.manager.config["parallel_video"] = False
    client = TestClient(app)
    return client, midi, calls, editor


def wait_status(client, job_id, status):
    for _ in range(300):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] == status:
            return body
        assert body["status"] not in {"error", "canceled"}, body
        time.sleep(0.02)
    raise AssertionError(body)


def submit_review(fixture, *, review=True, **fields):
    client, midi, _calls, _editor = fixture
    response = client.post(
        "/api/jobs", files={"midi": ("song.mid", midi, "audio/midi")},
        data={
            "wordlist_text": "静岡,シズオカ\n鈴鹿,スズカ\n清水,シミズ",
            "review_lyrics": str(review).lower(), **fields,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def resume(client, job_id, payload):
    return client.post(
        f"/api/jobs/{job_id}/resume",
        files={"editor": ("editor.json", json.dumps(payload), "application/json")},
    )


@pytest.mark.parametrize("input_kind", ["midi", "audio"])
def test_review_reuses_analysis_and_synthesizes_edited_lyrics(review_client, input_kind):
    client, midi, calls, _editor = review_client
    manager = client.app.state.manager
    # Audio inference is replaced at its boundary; the job pipeline and conversion run normally.
    if input_kind == "audio":
        job = manager.create(None, None, "", {
            "input_kind": "audio", "review_lyrics": True, "wordlist": "stations",
            "model": "MERROW", "song_title": "テスト曲",
        }, audio=b"audio-fixture")
        job_id = job.id
    else:
        job_id = submit_review(review_client)
        job = manager.get(job_id)
    paused = wait_status(client, job_id, "awaiting_review")
    assert [stage["name"] for stage in paused["stages"]] == ["analyze", "convert"]
    assert calls["analyze"] == 1
    assert calls["synthesize"] == []
    assert job.finished_at is None
    assert not job.usage_finished_recorded
    original_notes = Project.load(job.dir).notes
    response = client.get(f"/api/jobs/{job_id}/review")
    assert response.headers["cache-control"] == "private, no-store"
    payload = response.json()
    assert payload["results"] and payload["unitsList"]
    assert payload["noteLengthRawList"]
    assert payload["host"] == {"songs": [], "canUploadSong": False}
    if input_kind == "audio":
        assert payload["lyrics"] == "沈む"
    payload["results"][0][0]["surface"] = "編集した替え歌"
    response = resume(client, job_id, payload)
    assert response.status_code == 200, response.text
    wait_status(client, job_id, "done")
    assert calls["analyze"] == 1
    assert len(calls["synthesize"]) == 1
    project = calls["synthesize"][0]
    assert project.parody.lines[0].words[0].surface == "編集した替え歌"
    assert project.notes == original_notes
    assert resume(client, job_id, payload).status_code == 200
    assert len(calls["synthesize"]) == 1


def test_review_off_and_voice_preview_do_not_pause(review_client):
    client, _midi, calls, _editor = review_client
    for fields in ({"review": False}, {"preview": "20"}):
        job_id = submit_review(review_client, **fields)
        assert wait_status(client, job_id, "done")["params"]["review_lyrics"] is False
        assert client.get(f"/api/jobs/{job_id}/review").status_code == 409
    assert len(calls["synthesize"]) == 2


def test_waiting_review_releases_worker_and_can_be_canceled(review_client):
    client, _midi, calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    second = submit_review(review_client, review=False)
    wait_status(client, second, "done")
    assert len(calls["synthesize"]) == 1
    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.json()["status"] == "canceled"
    job = client.app.state.manager.get(job_id)
    assert list(job.dir.iterdir()) == [job.dir / api.STATUS_FILENAME]
    assert client.get(f"/api/jobs/{job_id}/review").status_code == 409
    assert resume(client, job_id, {}).status_code == 409


@pytest.mark.parametrize("change", ["phrases", "wordlist", "results", "period", "unitsList"])
def test_invalid_edits_leave_review_and_analysis_intact(review_client, change):
    client, _midi, calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    payload = client.get(f"/api/jobs/{job_id}/review").json()
    if change == "phrases":
        payload["phrases"] = ["ベツノキョク"]
    elif change == "wordlist":
        payload["wordlist"]["filepath"] = "/etc/passwd.csv"
    elif change == "period":
        payload["results"][0][0]["period"] = [-1, 100000]
    else:
        payload[change] = []
    assert resume(client, job_id, payload).status_code == 422
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "awaiting_review"
    assert Project.load(client.app.state.manager.get(job_id).dir).notes
    assert calls["synthesize"] == []


def test_review_survives_server_restart(review_client, tmp_path):
    client, _midi, calls, editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    app = api.create_app(jobs_dir=tmp_path / "jobs", editor_dist=editor)
    app.state.manager.config["parallel_video"] = False
    restarted = TestClient(app)
    assert restarted.get(f"/api/jobs/{job_id}").json()["status"] == "awaiting_review"
    payload = restarted.get(f"/api/jobs/{job_id}/review").json()
    assert resume(restarted, job_id, payload).status_code == 200
    wait_status(restarted, job_id, "done")
    assert calls["analyze"] == 1


def test_review_requires_owner_and_limits_upload(review_client, monkeypatch):
    client, _midi, _calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    client.get("/api/config")
    job = client.app.state.manager.get(job_id)
    job.owner = client.cookies.get(api.SESSION_COOKIE)
    stranger = TestClient(client.app)
    assert stranger.get(f"/api/jobs/{job_id}/review").status_code == 404
    assert resume(stranger, job_id, {}).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/review").status_code == 200
    response = client.post(f"/api/jobs/{job_id}/resume", files={
        "editor": ("editor.json", b" " * (MAX_REVIEW_BYTES + 1), "application/json"),
    })
    assert response.status_code == 413


def test_waiting_review_expires_and_removes_inputs(review_client, monkeypatch):
    client, _midi, _calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    manager = client.app.state.manager
    job = manager.get(job_id)
    assert (job.dir / REVIEW_FILENAME).is_file()
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, "1")
    assert manager.cleanup_expired(now=job.review_started_at + 3601) == [job_id]
    assert not job.dir.exists()


def test_review_unavailable_without_editor_or_in_simple_ui(review_client, monkeypatch):
    client, midi, _calls, editor = review_client
    monkeypatch.setenv(api.SIMPLE_UI_ENV, "1")
    assert client.get("/api/jobs/missing/review").status_code == 404
    assert resume(client, "missing", {}).status_code == 404
    monkeypatch.delenv(api.SIMPLE_UI_ENV)
    no_editor = TestClient(api.create_app(jobs_dir=editor / "jobs", editor_dist=editor / "missing"))
    response = no_editor.post("/api/jobs", files={"midi": ("song.mid", midi)}, data={
        "wordlist": "stations", "review_lyrics": "true",
    })
    assert response.status_code == 422
