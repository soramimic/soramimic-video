"""同梱editor連携(A-2)のテスト。

- POST /api/editor-session: MIDI+単語リスト→変換済みeditorセッションJSON
  (名前付きリストと、アップロードされた自作リストの両方)
- GET /editor/wordlists/{name}.csv: editorのDB構築が取りに来る単語リスト
- GET /editor/session-wordlists/{sid}.csv: 自作リストのeditorセッション
- GET /editor/conf/setting.json: editorのconf(ソース側の正データ)
- GET /api/config の editor 可否フラグ(dist の有無で切り替わる)
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from helpers import build_xf_midi

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402
from soramimic_video import convert as convert_mod  # noqa: E402

FAKE_MIDI = b"MThd" + b"\x00" * 16
FAKE_MP4 = b"fake-mp4-bytes"
# 自作リスト(貼り付けテキスト)。かんたん形式(1行1語 + 読み)
CUSTOM_TEXT = "静岡,シズオカ\n鈴鹿,スズカ\n清水,シミズ\n"


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


def _png(color: str = "red") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


@pytest.fixture
def job_client(tmp_path, monkeypatch):
    """/api/jobs まで通すクライアント(生成パイプラインは差し替える)。"""

    def fake_pipeline(job, config):
        out = job.dir / "video" / "song.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setattr(api_mod, "run_pipeline", fake_pipeline)
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def _custom_session(client, tmp_path, images=None, **extra):
    """自作リストで editor セッションを作り、返ってきた editor JSON を返す。"""
    midi = _xf_midi(tmp_path)
    files = [("midi", ("song.mid", midi.read_bytes(), "audio/midi"))]
    for name, data in (images or {}).items():
        files.append(("wordlist_images", (name, data, "image/png")))
    res = client.post(
        "/api/editor-session",
        files=files,
        data={"wordlist_text": CUSTOM_TEXT, "wordlist_name": "わたしの単語", **extra},
    )
    assert res.status_code == 200, res.text
    return res.json()


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


# ---- 自作リスト(アップロードされたCSV)でのeditorセッション ----


def test_editor_session_accepts_custom_wordlist(client, tmp_path):
    payload = _custom_session(client, tmp_path)
    entry = payload["wordlist"]
    sid = entry["value"].removeprefix("custom:")
    assert len(sid) == 16 and entry["value"].startswith("custom:")
    assert entry["text"] == "自作リスト"
    # editorのbuildDatabaseはこのfilepathを /editor/session-wordlists/<sid>.csv に解決する
    assert entry["filepath"] == f"session-wordlists/{sid}.csv"
    assert entry["dbtype"] == "tidy"
    assert "where" not in entry  # 自作リストに絞り込みは効かない
    assert payload["results"] and len(payload["unitsList"]) == len(payload["results"])
    # 正規化済みCSVはセッション置き場に残る(生成時の単語行の引き当てに使う)
    saved = tmp_path / "jobs" / "editor-sessions" / sid / "wordlist.csv"
    assert saved.read_text(encoding="utf-8").splitlines()[0].startswith("id,original,")


def test_editor_session_custom_id_is_deterministic(client, tmp_path):
    """同じ中身なら同じセッション(=開き直してもディレクトリが増えない)。"""
    first = _custom_session(client, tmp_path)["wordlist"]["value"]
    second = _custom_session(client, tmp_path)["wordlist"]["value"]
    assert first == second
    sessions = tmp_path / "jobs" / "editor-sessions"
    assert [p.name for p in sessions.iterdir()] == [first.removeprefix("custom:")]
    # sid は /api/wordlist-check が返す指紋と同じ(UIの来歴判定と揃う)
    check = client.post(
        "/api/wordlist-check", data={"wordlist_text": CUSTOM_TEXT}
    ).json()
    assert first == f"custom:{check['fingerprint']}"


def test_editor_session_custom_beats_wordlist_name(client, tmp_path):
    """自作リストとリスト名が両方来たら自作を優先する(/api/jobs と同じ)。"""
    payload = _custom_session(client, tmp_path, wordlist="definitely-not-a-real-list")
    assert payload["wordlist"]["value"].startswith("custom:")


def test_editor_session_rejects_broken_custom_wordlist(client, tmp_path):
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist_text": "surface,pronunciation\n猫,猫又\n"},
    )
    assert res.status_code == 400
    assert "カタカナ" in res.json()["detail"]


def test_session_wordlist_route(client, tmp_path):
    sid = _custom_session(
        client, tmp_path, images={"静岡.png": _png()}
    )["wordlist"]["value"].removeprefix("custom:")

    res = client.get(f"/editor/session-wordlists/{sid}.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    # editorのCSVパーサは行を素朴にsplitするので、末尾の改行があると落ちる
    assert not res.text.endswith("\n")
    lines = res.text.split("\n")
    assert lines[0] == "id,original,surface,pronunciation"  # image列は返さない
    assert len(lines) == 4 and "静岡" in lines[1]
    assert "/" not in res.text  # サーバー上の画像パスが漏れていない


def test_session_wordlist_route_validates_sid(client, tmp_path):
    _custom_session(client, tmp_path)
    for sid in ("unknown", "0" * 16, "..%2f..%2fetc%2fpasswd", "../wordlist"):
        assert client.get(f"/editor/session-wordlists/{sid}.csv").status_code == 404


def test_editor_session_passes_convert_params(client, tmp_path, monkeypatch):
    """変換パラメータ(convert_params)が変換に渡る(名前付き・自作の両方)。"""
    real = convert_mod.convert_project
    seen: dict = {}

    def spy(project, wordlist, where=None, params=None, cache_db=True):
        seen.update(wordlist=wordlist, where=where, params=params, cache_db=cache_db)
        return real(project, wordlist, where, params, cache_db)

    monkeypatch.setattr(convert_mod, "convert_project", spy)

    _custom_session(client, tmp_path, convert_params="DUPLICATE=true;VOWEL_RATIO=0.5")
    assert seen["params"] == {"DUPLICATE": "true", "VOWEL_RATIO": "0.5"}
    assert seen["where"] is None          # 自作リストに絞り込みは無い
    assert seen["cache_db"] is False      # この入力限りのリストは共有キャッシュに載せない
    assert seen["wordlist"].endswith("wordlist.csv")

    seen.clear()
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={
            "wordlist": str(_wordlist_csv(tmp_path)),
            "convert_params": "NOTE_LENGTH_WEIGHT=0.5",
        },
    )
    assert res.status_code == 200, res.text
    assert seen["params"] == {"NOTE_LENGTH_WEIGHT": "0.5"}
    assert seen["cache_db"] is True


def test_import_editor_resolves_custom_session(client, tmp_path):
    """custom:<sid> のeditor JSONから、単語画像つきの単語リスト行が引ける。"""
    from soramimic_video.editor_io import import_editor
    from soramimic_video.xfparse import analyze_midi

    payload = _custom_session(client, tmp_path, images={"静岡.png": _png()})
    # editorでの編集をシミュレート: 先頭単語を画像つきの「静岡」に差し替える
    payload["results"][0][0] = dict(
        payload["results"][0][0],
        surface="静岡", kana="シズオカ", original="静岡", id="1",
        pronunciation=["シ", "ズ", "オ", "カ"],
    )
    d = tmp_path / "project"
    d.mkdir()
    (d / "editor.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    project = analyze_midi(_xf_midi(tmp_path))
    import_editor(
        project, d, d / "editor.json",
        sessions_dir=tmp_path / "jobs" / "editor-sessions",
    )
    row = project.parody.lines[0].words[0].wordlist_row
    assert row is not None and row["surface"] == "静岡"
    # 動画側は image 列をローカルパスとして読む(実体が置かれている)
    assert Path(row["image"]).is_file()


def test_import_editor_without_session_fails(client, tmp_path):
    """セッションが消えている(サーバー再構築など)ときは黙って別のリストを使わない。"""
    from soramimic_video.editor_io import import_editor
    from soramimic_video.xfparse import analyze_midi

    payload = _custom_session(client, tmp_path)
    payload["wordlist"]["value"] = "custom:" + "a" * 16
    d = tmp_path / "project"
    d.mkdir()
    (d / "editor.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    project = analyze_midi(_xf_midi(tmp_path))
    with pytest.raises(ValueError, match="自作リスト"):
        import_editor(
            project, d, d / "editor.json",
            sessions_dir=tmp_path / "jobs" / "editor-sessions",
        )


def test_job_accepts_custom_session_editor(job_client, tmp_path):
    """自作リストで作ったeditor JSONは、リストを再添付しなくても投入できる。"""
    payload = _custom_session(job_client, tmp_path)
    res = job_client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "editor": ("editor.json", json.dumps(payload).encode(), "application/json"),
        },
    )
    assert res.status_code == 200, res.text
    params = job_client.get(f"/api/jobs/{res.json()['id']}").json()["params"]
    assert params["wordlist"] == "自作リスト"  # 履歴・ダウンロード名に出る表示名
    assert params["parody_source"] == "editor"


def test_job_rejects_editor_with_unknown_session(job_client, tmp_path):
    payload = {
        "results": [],
        "unitsList": [],
        "wordlist": {"value": "custom:" + "b" * 16, "filepath": "session-wordlists/x.csv"},
    }
    res = job_client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "editor": ("editor.json", json.dumps(payload).encode(), "application/json"),
        },
    )
    assert res.status_code == 422
    assert "自作リスト" in res.json()["detail"]


def test_setting_json_from_source_conf(client):
    """/editor/conf/setting.json は dist ではなくソース側の conf を返す。"""
    from soramimic_video.editor_io import SETTING_JSON

    if not SETTING_JSON.is_file():
        pytest.skip("external/soramimic のsubmoduleが無い環境")

    res = client.get("/editor/conf/setting.json")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == json.loads(SETTING_JSON.read_text(encoding="utf-8"))


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
