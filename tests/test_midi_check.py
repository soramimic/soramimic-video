"""POST /api/midi-check(選んだMIDIの歌詞チェック)のテスト。

UIがファイル選択の直後にこれを呼び、歌詞の無いMIDIをその場で断る。
元歌詞を一緒に渡したときは、字幕と同じ割り付け(align_lines)の結果として
「元歌詞が対応づかなかったXF行」の数を返す。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import build_xf_midi

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def _xf_midi(tmp_path: Path) -> bytes:
    """2行(「沈む」/「とけ」)の歌詞入りXF MIDI。"""
    path = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64), (960, 240, 65), (1200, 240, 67)],
        lyric_events=[(0, "沈[しず"), (240, "ず"), (480, "]む"), (960, "/と"), (1200, "け")],
    )
    return path.read_bytes()


def _no_lyrics_midi(tmp_path: Path) -> bytes:
    """音符だけで歌詞イベントが1つも無いMIDI(XFKMはあるが空)。"""
    path = build_xf_midi(
        tmp_path / "nolyrics.mid",
        notes=[(0, 240, 60), (240, 240, 62)],
        lyric_events=[],
    )
    return path.read_bytes()


def _post(client, midi: bytes, lyrics: str = ""):
    return client.post(
        "/api/midi-check",
        files={"midi": ("song.mid", midi, "audio/midi")},
        data={"lyrics": lyrics},
    )


def test_reports_lyrics(client, tmp_path):
    res = _post(client, _xf_midi(tmp_path))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_lyrics"] is True
    assert body["lines"] == 2
    # 元歌詞を渡していないので不整合の判定はしない
    assert body["lyrics_lines"] == 0
    assert body["unmatched_lines"] == 0


def test_rejects_midi_without_lyrics(client, tmp_path):
    res = _post(client, _no_lyrics_midi(tmp_path))
    # 歌詞なしはエラーではなく「使えない」という判定結果として返す(UIが理由を出す)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_lyrics"] is False
    assert body["lines"] == 0
    assert body["midi_lines"] == []
    assert body["detail"]


def test_rejects_non_midi(client):
    res = _post(client, b"not a midi at all")
    assert res.status_code == 400
    assert "MIDI" in res.json()["detail"]


def test_returns_midi_lines_for_prefill(client, tmp_path):
    """XF歌詞の行テキスト(UIが元歌詞欄の下敷きに使う)を行順に返す。"""
    path = build_xf_midi(
        tmp_path / "plain.mid",
        notes=[(0, 240, 60), (240, 240, 62), (960, 240, 64), (1200, 240, 65)],
        lyric_events=[(0, "は"), (240, "る"), (960, "/な"), (1200, "つ")],
    )
    res = _post(client, path.read_bytes())
    assert res.status_code == 200, res.text
    assert res.json()["midi_lines"] == ["はる", "なつ"]


def test_matching_lyrics_have_no_unmatched_lines(client, tmp_path):
    res = _post(client, _xf_midi(tmp_path), lyrics="沈む\nとけ\n")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["lines"] == 2
    assert body["lyrics_lines"] == 2
    assert body["unmatched_lines"] == 0


def test_unrelated_lyrics_are_reported_as_unmatched(client, tmp_path):
    res = _post(client, _xf_midi(tmp_path), lyrics="ぱぴぷぺぽ\nがぎぐげご\n")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["lines"] == 2
    assert body["lyrics_lines"] == 2
    # 字幕の元歌詞がどの行にも付かない=UIが警告を出す状態
    assert body["unmatched_lines"] == 2


def test_requires_api_key(client, tmp_path, monkeypatch):
    monkeypatch.setenv(api_mod.API_KEY_ENV, "secret-key")
    midi = _xf_midi(tmp_path)
    assert _post(client, midi).status_code == 401
    res = client.post(
        "/api/midi-check",
        files={"midi": ("song.mid", midi, "audio/midi")},
        headers={"X-API-Key": "secret-key"},
    )
    assert res.status_code == 200, res.text
