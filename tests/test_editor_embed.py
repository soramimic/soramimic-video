"""同梱editor連携(A-2)のテスト。

- POST /api/editor-session: MIDI+単語リスト→変換済みeditorセッションJSON
- GET /editor/wordlists/{name}.csv: editorのDB構築が取りに来る単語リスト
- GET /api/config の editor 可否フラグ(dist の有無で切り替わる)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import build_xf_midi

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video import convert as convert_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16


def _wordlist_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n"
        "0,静岡駅,静岡,シズオカ\n"
        "1,鈴鹿,鈴鹿,スズカ\n"
        "2,清水,清水,シミズ",
        encoding="utf-8",
    )
    return csv_path


def _xf_midi(tmp_path: Path) -> Path:
    return build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64)],
        lyric_events=[(0, "し"), (240, "ず"), (480, "む")],
    )


@pytest.fixture
def client(tmp_path):
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def test_editor_session_happy_path(client, tmp_path):
    midi = _xf_midi(tmp_path)
    wordlist = _wordlist_csv(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(wordlist), "lyrics": "静けさ\n"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["format"] == "soramimic-editor/1"
    assert isinstance(payload["results"], list) and payload["results"]
    assert len(payload["unitsList"]) == len(payload["results"])
    # editorのDB構築はこの filepath を /editor/wordlists/<stem>.csv として取りに来る
    assert payload["wordlist"]["filepath"] == "wordlists/words.csv"


def test_editor_session_requires_wordlist(client, tmp_path):
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
    )
    assert res.status_code == 422


def test_editor_session_rejects_broken_midi(client, tmp_path):
    wordlist = _wordlist_csv(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", b"not-a-midi", "audio/midi")},
        data={"wordlist": str(wordlist)},
    )
    assert res.status_code == 400


def test_editor_session_unknown_wordlist(client, tmp_path):
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": "definitely-not-a-real-list"},
    )
    assert res.status_code == 404


def test_wordlist_csv_route(client, tmp_path, monkeypatch):
    csv_path = _wordlist_csv(tmp_path)

    def fake_resolve(name: str) -> Path:
        if name == "mylist":
            return csv_path
        raise FileNotFoundError(name)

    monkeypatch.setattr(convert_mod, "resolve_wordlist", fake_resolve)

    res = client.get("/editor/wordlists/mylist.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "静岡" in res.text

    assert client.get("/editor/wordlists/unknown.csv").status_code == 404


def test_config_editor_flag_true(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "editor.html").write_text("<html></html>", encoding="utf-8")
    client = TestClient(
        api_mod.create_app(jobs_dir=tmp_path / "jobs", editor_dist=dist)
    )
    assert client.get("/api/config").json()["editor"] is True
    # 静的マウントが有効: editor.html が引ける
    assert client.get("/editor/editor.html").status_code == 200


def test_config_editor_flag_false(tmp_path):
    client = TestClient(
        api_mod.create_app(jobs_dir=tmp_path / "jobs", editor_dist=tmp_path / "nope")
    )
    assert client.get("/api/config").json()["editor"] is False
    # dist が無ければ /editor 配下は配信されない
    assert client.get("/editor/editor.html").status_code == 404
