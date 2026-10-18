"""Existing review jobs remain accessible; newly submitted jobs do not pause."""

from __future__ import annotations

import copy
import json
import time
from unittest.mock import patch

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
    # Seed the legacy pipeline flag as an older saved job would have it.
    # The current upload API no longer enables review from form data.
    manager = client.app.state.manager
    create = manager.create

    def legacy_create(midi, editor, lyrics, params, **kwargs):
        params["review_lyrics"] = review and float(fields.get("preview", 0)) <= 0
        return create(midi, editor, lyrics, params, **kwargs)

    with patch.object(manager, "create", legacy_create):
        response = client.post(
            "/api/jobs", files={"midi": ("song.mid", midi, "audio/midi")},
            data={
                "wordlist_text": "静岡,シズオカ\n鈴鹿,スズカ\n清水,シミズ",
                **fields,
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
    assert payload["host"] == {"songs": [], "canUploadSong": False, "review": True}
    if input_kind == "audio":
        assert payload["lyrics"] == "沈む"
        assert payload["originalLines"] == ["沈む"]
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


@pytest.mark.parametrize("change", [
    "phrases", "wordlist", "results", "period", "unitsList", "tokensList",
    "lyrics", "originalLines",
])
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


def test_review_draft_survives_restart_without_starting_generation(review_client, tmp_path):
    client, _midi, calls, editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    payload = client.get(f"/api/jobs/{job_id}/review").json()
    payload["results"][0][0]["surface"] = "保存した歌詞"
    response = client.put(f"/api/jobs/{job_id}/review", files={
        "editor": ("editor.json", json.dumps(payload), "application/json"),
    })
    assert response.status_code == 200, response.text
    assert calls["synthesize"] == []
    app = api.create_app(jobs_dir=tmp_path / "jobs", editor_dist=editor)
    app.state.manager.config["parallel_video"] = False
    restarted = TestClient(app)
    assert restarted.get(f"/api/jobs/{job_id}").json()["status"] == "awaiting_review"
    saved = restarted.get(f"/api/jobs/{job_id}/review").json()
    assert saved["results"][0][0]["surface"] == "保存した歌詞"
    assert saved["unitsList"] == payload["unitsList"]
    assert resume(restarted, job_id, saved).status_code == 200
    wait_status(restarted, job_id, "done")
    assert calls["analyze"] == 1
    assert calls["synthesize"][0].parody.lines[0].words[0].surface == "保存した歌詞"


def test_partial_review_can_be_saved_but_must_cover_all_notes_before_resuming(
    review_client, monkeypatch,
):
    from soramimic_video import convert

    # Exercise the stricter note-identity path used by analyzed singing audio.
    monkeypatch.setattr(
        convert, "_layer_unit_note_indices",
        lambda _project, _line, units: [[i] for i in range(len(units))],
    )
    client, _midi, calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    payload = client.get(f"/api/jobs/{job_id}/review").json()
    units = payload["unitsList"][0]
    assert len(units) == 3
    kana = units[1]["pronunciation"]
    fixed = {
        "id": "custom-fixed", "surface": "固定語", "kana": kana,
        "pronunciation": [kana], "period": [1, 2], "locked": True,
    }
    payload["results"] = [[fixed]]
    response = client.put(f"/api/jobs/{job_id}/review", files={
        "editor": ("editor.json", json.dumps(payload), "application/json"),
    })
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/jobs/{job_id}/review").json()
    assert saved["results"] == [[fixed]]  # Validation placeholders are never saved.
    assert saved["unitsList"] == payload["unitsList"]
    response = resume(client, job_id, saved)
    assert response.status_code == 422
    assert "未対応" in response.json()["detail"]
    assert calls["synthesize"] == []
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "awaiting_review"
    saved["results"][0] = [
        fixed if i == 1 else {
            "surface": unit["pronunciation"], "kana": unit["pronunciation"],
            "pronunciation": unit["pronunciation"], "period": [i, i + 1], "filler": True,
        }
        for i, unit in enumerate(units)
    ]
    assert resume(client, job_id, saved).status_code == 200
    wait_status(client, job_id, "done")
    words = calls["synthesize"][0].parody.lines[0].words
    assert [word.filler for word in words] == [True, False, True]
    assert words[1].surface == "固定語" and words[1].locked
    assert calls["analyze"] == 1


def test_review_draft_checks_owner_source_and_job_state(review_client, monkeypatch):
    client, _midi, calls, _editor = review_client
    job_id = submit_review(review_client)
    wait_status(client, job_id, "awaiting_review")
    payload = client.get(f"/api/jobs/{job_id}/review").json()

    def save(using_client, body):
        return using_client.put(f"/api/jobs/{job_id}/review", files={
            "editor": ("editor.json", json.dumps(body), "application/json"),
        })

    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    client.get("/api/config")
    client.app.state.manager.get(job_id).owner = client.cookies.get(api.SESSION_COOKIE)
    assert save(TestClient(client.app), payload).status_code == 404
    changed = copy.deepcopy(payload)
    changed["lyrics"] = "別の歌詞"
    assert save(client, changed).status_code == 422
    assert client.get(f"/api/jobs/{job_id}/review").json() == payload
    response = client.put(f"/api/jobs/{job_id}/review", files={
        "editor": ("editor.json", b" " * (MAX_REVIEW_BYTES + 1), "application/json"),
    })
    assert response.status_code == 413
    client.post(f"/api/jobs/{job_id}/cancel")
    assert save(client, payload).status_code == 409
    assert calls["synthesize"] == []


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
    no_editor.app.state.manager.config["parallel_video"] = False
    response = no_editor.post("/api/jobs", files={"midi": ("song.mid", midi)}, data={
        "wordlist": "stations", "review_lyrics": "true",
    })
    assert response.status_code == 200
    job = wait_status(no_editor, response.json()["id"], "done")
    assert job["params"]["review_lyrics"] is False


@pytest.mark.parametrize("input_kind", ["midi", "audio"])
def test_completed_video_keeps_portable_edited_lyrics(review_client, monkeypatch, input_kind):
    from soramimic_video.completed_lyrics import FILENAME

    client, _midi, calls, editor = review_client
    manager = client.app.state.manager
    manager.config["scrub_private_artifacts"] = True
    if input_kind == "audio":
        job_id = manager.create(None, None, "", {
            "input_kind": "audio", "review_lyrics": True, "wordlist": "stations",
            "model": "MERROW", "song_title": "テスト曲",
        }, audio=b"audio-fixture").id
    else:
        job_id = submit_review(review_client)
    manager.get(job_id).params.update(
        song_title="テスト曲", song_credit="作詞・作曲: テスト", thumbnail_wordlist_label="表示名",
    )
    wait_status(client, job_id, "awaiting_review")
    payload = client.get(f"/api/jobs/{job_id}/review").json()
    payload["results"][0][0].update(surface="編集した替え歌", locked=True)
    assert resume(client, job_id, payload).status_code == 200
    body = wait_status(client, job_id, "done")
    url = body["editor_url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert result["results"][0][0]["surface"] == "編集した替え歌"
    assert result["results"][0][0]["locked"] is True
    assert result["unitsList"] == payload["unitsList"]
    assert result["noteLengthRawList"]
    assert result["videoMetadata"] == {
        "songTitle": "テスト曲", "songCredit": "作詞・作曲: テスト",
        "thumbnailWordlistLabel": "表示名",
    }
    assert "host" not in result
    assert "history" not in result
    assert str(manager.jobs_dir) not in response.text
    if input_kind == "audio":
        assert result["lyrics"] == "沈む"
        assert result["wordlist"]["filepath"] == "wordlists/stations.csv"
    else:
        assert result["wordlist"]["value"] == "ORIGINAL"
        assert "清水" in result["wordlist"]["csvText"]
    job = manager.get(job_id)
    assert sorted(p.name for p in job.dir.iterdir()) == [
        "exports", FILENAME, "status.json", "video.mp4",
    ]
    assert set(body["export_urls"]) == {"musicxml", "midi", "xf", "vocal"}
    assert client.get(body["export_urls"]["vocal"]).content == b"voice"
    assert len(calls["synthesize"]) == 1

    # The same owner check protects both the history link and its JSON endpoint.
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    client.get("/api/config")
    job.owner = client.cookies.get(api.SESSION_COOKIE)
    manager._save(job)
    stranger = TestClient(client.app)
    assert stranger.get(url).status_code == 404
    assert client.get(url).status_code == 200
    restarted = TestClient(api.create_app(jobs_dir=manager.jobs_dir, editor_dist=editor))
    restarted.cookies.update(client.cookies)
    assert restarted.get(url).json() == result
    assert FILENAME in [p.name for p in job.dir.iterdir()]
    legacy = dict(result)
    del legacy["videoMetadata"]
    (job.dir / FILENAME).write_text(json.dumps(legacy), encoding="utf-8")
    assert restarted.get(url).json() == result
    assert json.loads((job.dir / FILENAME).read_text()) == legacy
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, "1")
    restored = restarted.app.state.manager
    restored.get(job_id).finished_at = time.time() - 7200
    assert restored.cleanup_expired() == [job_id]
    assert restarted.get(url).status_code == 404
    assert not job.dir.exists()


@pytest.mark.parametrize("alpha", [0.0, 0.25, 1.0])
def test_completed_lyrics_preserves_note_length_weight_through_reload_and_import(
    review_client, alpha,
):
    from soramimic_video.completed_lyrics import save_result

    client, midi, _calls, _editor = review_client
    job_id = submit_review(
        review_client, review=False, convert_params=f"NOTE_LENGTH_WEIGHT={alpha}",
    )
    job = wait_status(client, job_id, "done")
    payload = client.get(job["editor_url"]).json()
    assert payload["noteLengthAlpha"] == alpha
    assert payload["noteLengthRawList"]
    assert "NOTE_LENGTH_WEIGHT" not in payload["param"]

    directory = client.app.state.manager.get(job_id).dir
    reloaded = Project.load(directory)
    assert reloaded.parody.note_length_alpha == alpha
    save_result(reloaded, directory)
    assert client.get(job["editor_url"]).json()["noteLengthAlpha"] == alpha

    # An editor can change the setting; a later Video request must keep it,
    # rather than replacing it with the form's conversion default.
    payload["noteLengthAlpha"] = 0.75
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", json.dumps(payload), "application/json"),
    }, data={"convert_params": "NOTE_LENGTH_WEIGHT=0.25"})
    assert response.status_code == 200, response.text
    imported = wait_status(client, response.json()["id"], "done")
    exported = client.get(imported["editor_url"]).json()
    assert exported["noteLengthAlpha"] == 0.75
    assert exported["results"] == payload["results"]


def test_completed_lyrics_omits_large_results_and_unsafe_files(
    review_client, monkeypatch, tmp_path,
):
    from soramimic_video import completed_lyrics

    client, _midi, _calls, _editor = review_client
    monkeypatch.setattr(completed_lyrics, "MAX_BYTES", 8)
    job_id = submit_review(review_client, review=False)
    body = wait_status(client, job_id, "done")
    assert "editor_url" not in body
    manager = client.app.state.manager
    job = manager.get(job_id)
    result = job.dir / completed_lyrics.FILENAME
    assert not result.exists()
    outside = tmp_path / "private.json"
    outside.write_text("secret")
    result.symlink_to(outside)
    assert "editor_url" not in job.to_dict()
    assert client.get(f"/api/jobs/{job_id}/lyrics").status_code == 404
    manager._cleanup_completed_artifacts(job)
    assert not result.is_symlink()
    assert outside.read_text() == "secret"
    result.write_text("result")
    manager._cleanup_failed_artifacts(job)
    assert not result.exists()



def test_completed_lyrics_accepts_relative_custom_wordlist(review_client, monkeypatch, tmp_path):
    from pathlib import Path

    from soramimic_video.completed_lyrics import FILENAME, save_result

    client, _midi, _calls, _editor = review_client
    job_id = submit_review(review_client, review=False)
    wait_status(client, job_id, "done")
    directory = client.app.state.manager.get(job_id).dir
    project = Project.load(directory)
    project.parody.wordlist = str(Path(project.parody.wordlist).relative_to(tmp_path))
    monkeypatch.chdir(tmp_path)
    save_result(project, directory)
    payload = json.loads((directory / FILENAME).read_text())
    assert payload["wordlist"]["value"] == "ORIGINAL"
    assert "清水" in payload["wordlist"]["csvText"]
