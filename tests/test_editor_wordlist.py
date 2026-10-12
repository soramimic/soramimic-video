"""Unknown lists must not change imported lyrics or trigger image lookups."""

from __future__ import annotations

import copy
import csv
import io
import json

import pytest

from soramimic_video import api, editor_completion
from soramimic_video.editor_wordlist import text_only_import
from soramimic_video.layout import ImageElement, load_layout
from soramimic_video.video import collect_word_frames
from test_lyric_review import review_client as review_client
from test_lyric_review import wait_status
from test_public_mode import fake_wav


def lyrics():
    return {
        "format": "soramimic-editor/1", "phrases": ["シズム"], "lyrics": "沈む",
        "wordlist": {"value": "UNKNOWN", "text": "新しいリスト",
                     "filepath": "wordlists/not-installed.csv", "where": "type=one"},
        "where": "type=one", "param": {"DUPLICATE": False},
        "unitsList": [[{"pronunciation": kana} for kana in "シズム"]],
        "results": [[
            {"id": "7", "surface": "空耳,テスト", "kana": "ソラミミテスト",
             "pronunciation": "ソラミミテスト", "period": [0, 2], "locked": True,
             "image": "https://example.invalid/unwanted.png"},
            {"surface": "む", "kana": "ム", "pronunciation": "ム",
             "period": [2, 3], "filler": True, "locked": False},
        ]],
    }


def test_snapshot_preserves_words_and_only_carries_text_columns():
    source = lyrics()
    original = copy.deepcopy(source)
    result = text_only_import(source)
    assert source == original
    assert result["results"] == source["results"]
    assert result["unitsList"] == source["unitsList"]
    assert "where" not in result
    entry = result["wordlist"]
    assert entry["textOnly"] is True
    assert entry["value"] == "ORIGINAL"
    assert entry["text"] == "新しいリスト"
    rows = list(csv.DictReader(io.StringIO(entry["csvText"])))
    assert list(rows[0]) == ["id", "original", "surface", "pronunciation"]
    assert rows[0]["id"] == "7"
    assert rows[0]["pronunciation"] == source["results"][0][0]["kana"]
    assert text_only_import(result) == result


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("kind", ["midi", "audio"])
def test_missing_wordlist_sings_exact_words_and_fillers_without_images(
    review_client, monkeypatch, public, kind,
):
    client, midi, calls, _ = review_client
    payload = lyrics()
    monkeypatch.setenv(api.PUBLIC_ENV, "1" if public else "0")
    monkeypatch.setattr(api, "audio_input_available", lambda: True)
    client.app.state.manager.config["scrub_private_artifacts"] = public

    def reconversion(*args, **kwargs):
        pytest.fail("Text-only imports must keep fillers and never regenerate lyrics")

    monkeypatch.setattr(editor_completion, "run_convert", reconversion)
    files = {"midi": ("song.mid", midi)} if kind == "midi" else {
        "audio": ("song.wav", fake_wav(2), "audio/wav"),
    }
    files["editor"] = ("lyrics.json", json.dumps(payload), "application/json")
    response = client.post("/api/jobs", files=files, data={
        "wordlist": "stations", "layout": "pokemon_card", "layout_json": "{invalid}",
    })
    assert response.status_code == 200, response.text
    job = wait_status(client, response.json()["id"], "done")
    assert job["params"]["layout"] == "lyrics_text"
    assert job["params"]["wordlist_label"] == "新しいリスト"
    assert job["params"]["text_only_wordlist"] is True
    project = calls["synthesize"][0]
    words = project.parody.lines[0].words
    assert [word.surface for word in words] == ["空耳,テスト", "む"]
    assert [word.kana for word in words] == ["ソラミミテスト", "ム"]
    assert ["".join(word.note_kana) for word in words] == ["ソラミミテスト", "ム"]
    assert [word.filler for word in words] == [False, True]
    layout = load_layout(job["params"]["layout"])
    assert not any(isinstance(element, ImageElement) for element in layout.elements)
    frames = collect_word_frames(project, layout)
    assert [frame.data["surface"] for frame in frames] == [word.surface for word in words]
    assert not any(frame.data.get("image") for frame in frames)
    exported = client.get(job["editor_url"]).json()
    assert exported["wordlist"]["textOnly"] is True
    assert exported["wordlist"]["text"] == "新しいリスト"
    assert exported["results"] == payload["results"]
    # Re-importing completed lyrics must retain the same semantics, even after
    # server-side private inputs have been removed.
    again = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", json.dumps(exported), "application/json"),
    })
    assert again.status_code == 200, again.text
    wait_status(client, again.json()["id"], "done")
    assert [word.kana for word in calls["synthesize"][1].parody.lines[0].words] == [
        "ソラミミテスト", "ム",
    ]


@pytest.mark.parametrize("change", ["missing_kana", "gap", "overlap", "empty_words"])
def test_invalid_fallback_stops_before_analysis(review_client, monkeypatch, change):
    client, midi, calls, _ = review_client
    payload = lyrics()
    if change == "missing_kana":
        payload["results"][0][0].pop("kana")
    elif change == "gap":
        payload["results"][0][0]["period"] = [1, 2]
    elif change == "overlap":
        payload["results"][0][1]["period"] = [1, 3]
    else:
        payload["results"][0] = []
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    response = client.post("/api/jobs", files={
        "midi": ("song.mid", midi),
        "editor": ("lyrics.json", json.dumps(payload), "application/json"),
    })
    assert response.status_code == 422
    assert not client.app.state.manager.jobs
    assert calls["analyze"] == 0


def test_hidden_but_installed_list_uses_text_only_and_catalog_matches_ui(monkeypatch):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setattr(api, "launch_wordlist_names", lambda: {"stations"})
    assert api.editor_import_wordlists() == ["stations"]
    payload = lyrics()
    payload["wordlist"]["filepath"] = "wordlists/pokemon.csv"
    assert api.prepare_editor_import(payload)["wordlist"]["textOnly"] is True
    payload["wordlist"]["filepath"] = "/old/path/stations.csv"
    result = api.prepare_editor_import(payload)
    assert result["wordlist"] == {"filepath": "stations.csv"}
    assert result["results"] == payload["results"]


def test_preview_uses_the_same_text_only_words(review_client, monkeypatch):
    client, _, _, _ = review_client
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    response = client.post("/api/editor-preview", files={
        "editor": ("lyrics.json", json.dumps(lyrics()), "application/json"),
    }, data={"wordlist": "stations", "layout_json": "{invalid}"})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["total"] == 2
    assert preview["data"]["surface"] == "空耳,テスト"
    assert preview["data"]["kana"] == "ソラミミテスト"
    assert not preview["image_url"]
    assert not preview["data"].get("image")
