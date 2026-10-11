"""Portable lyric exports run straight through the video pipeline."""
from __future__ import annotations

import json

import pytest

from soramimic_video import api
from soramimic_video.editor_io import MAX_EDITOR_BYTES
from test_lyric_review import review_client as review_client
from test_lyric_review import wait_status
from test_public_mode import fake_wav


def exported_lyrics(client, midi, custom=False):
    fields = {"wordlist_text": "静岡,シズオカ\n鈴鹿,スズカ\n清水,シミズ"} if custom else {
        "wordlist": "stations",
    }
    response = client.post("/api/editor-session", files={"midi": ("song.mid", midi)}, data=fields)
    assert response.status_code == 200, response.text
    payload = response.json()
    # The standalone editor omits host state when exporting its JSON.
    payload.pop("host", None)
    payload.pop("hostRequest", None)
    payload["results"][0][0]["surface"] = "保存した替え歌"
    return payload


@pytest.mark.parametrize("kind", ["midi", "audio"])
def test_new_jobs_ignore_old_review_option_and_finish(review_client, monkeypatch, kind):
    client, midi, calls, _ = review_client
    monkeypatch.setattr(api, "audio_input_available", lambda: True)
    files = {kind: ("song.mid", midi)} if kind == "midi" else {
        "audio": ("song.wav", fake_wav(2), "audio/wav"),
    }
    response = client.post("/api/jobs", files=files, data={
        "wordlist": "stations", "review_lyrics": "true",
    })
    assert response.status_code == 200, response.text
    job = wait_status(client, response.json()["id"], "done")
    assert job["params"]["review_lyrics"] is False
    assert len(calls["synthesize"]) == 1
    directory = client.app.state.manager.get(job["id"]).dir
    assert not (directory / "lyric-review.json").exists()


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_exported_lyrics_are_sung_without_reconversion(review_client, monkeypatch, public, custom):
    client, midi, calls, _ = review_client
    payload = exported_lyrics(client, midi, custom)
    monkeypatch.setenv(api.PUBLIC_ENV, "1" if public else "0")
    client.app.state.manager.config["scrub_private_artifacts"] = public
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("soramimic-edit.json", json.dumps(payload), "application/json"),
    }, data={"wordlist": "stations", "review_lyrics": "true"})
    assert response.status_code == 200, response.text
    job = wait_status(client, response.json()["id"], "done")
    stages = [stage["name"] for stage in job["stages"]]
    assert "import-editor" in stages and "convert" not in stages
    assert calls["synthesize"][0].parody.lines[0].words[0].surface == "保存した替え歌"
    assert job["params"]["review_lyrics"] is False
    if public:
        directory = client.app.state.manager.get(job["id"]).dir
        assert not (directory / "input.mid").exists()
        assert not (directory / "editor.json").exists()
        assert not (directory / "project.json").exists()


@pytest.mark.parametrize("raw", [
    b"{}", b"[]", b"null", b"{", b"\xff", b'{"results":[[]],"unitsList":[]}',
])
def test_invalid_lyrics_file_is_rejected_before_job_creation(review_client, raw):
    client, midi, calls, _ = review_client
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi), "editor": ("lyrics.json", raw, "application/json"),
    }, data={"wordlist": "stations"})
    assert response.status_code == 422
    assert not client.app.state.manager.jobs
    assert calls["analyze"] == 0


def test_large_lyrics_file_is_rejected(review_client):
    client, midi, _, _ = review_client
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", b" " * (MAX_EDITOR_BYTES + 1), "application/json"),
    }, data={"wordlist": "stations"})
    assert response.status_code == 413
    assert not client.app.state.manager.jobs


def test_public_import_uses_export_wordlist_and_never_a_supplied_path(review_client, monkeypatch):
    client, midi, calls, _ = review_client
    payload = exported_lyrics(client, midi)
    payload["wordlist"]["filepath"] = "/private/path/stations.csv"
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", json.dumps(payload), "application/json"),
    }, data={"wordlist": "pokemon"})
    assert response.status_code == 200, response.text
    job = wait_status(client, response.json()["id"], "done")
    assert job["params"]["wordlist"] == "stations"
    assert calls["synthesize"][0].parody.wordlist == "stations.csv"


def test_public_import_rejects_unknown_export_wordlist(review_client, monkeypatch):
    client, midi, calls, _ = review_client
    payload = exported_lyrics(client, midi)
    payload["wordlist"]["filepath"] = "/private/path/unknown.csv"
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", json.dumps(payload), "application/json"),
    }, data={"wordlist": "stations"})
    assert response.status_code == 422
    assert calls["synthesize"] == []
