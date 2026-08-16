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

# job_client フィクスチャが api_mod.run_pipeline を差し替えるので、本物を先に押さえる
REAL_RUN_PIPELINE = api_mod.run_pipeline

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
    assert seen["params"] == {
        "DUPLICATE": "true",
        "VOWEL_RATIO": "0.5",
        "NOTE_LENGTH_WEIGHT": "0.25",
    }
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


def test_editor_session_note_length_weights(client, tmp_path):
    """videoは生重みを渡し、αの設定と指数計算をsoramimicへ委ねる。"""
    midi = _xf_midi(tmp_path)
    wordlist = _wordlist_csv(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(wordlist), "convert_params": "NOTE_LENGTH_WEIGHT=1"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    weights = payload["weightsList"]
    raw = payload["noteLengthRawList"]
    assert payload["noteLengthAlpha"] == 1
    assert raw == weights
    assert len(raw) == len(payload["unitsList"])
    for row, units in zip(raw, payload["unitsList"], strict=True):
        assert len(row) == len(units)
        assert all(isinstance(w, (int, float)) and w >= 0 for w in row)

    # αの指定がなければ、soramimic UIの既定0.25を初期値として渡す。
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(wordlist)},
    )
    assert res.status_code == 200, res.text
    default = res.json()
    assert default["noteLengthAlpha"] == 0.25
    assert default["noteLengthRawList"]
    assert default["weightsList"]

    # 明示的な0はオフ。生重みは残すのでsoramimicで再び有効化できる。
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={
            "wordlist": str(wordlist),
            "convert_params": "NOTE_LENGTH_WEIGHT=0",
        },
    )
    off = res.json()
    assert off["noteLengthAlpha"] == 0
    assert off["noteLengthRawList"]
    assert "weightsList" not in off


# ---- 解析のみモード(convert=0): editor のセットアップ画面から始めるシード ----


def _setup_seed(client, tmp_path, **extra):
    """解析のみモードで editor シードを取る。"""
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"convert": "0", **extra},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_setup_seed_has_phrases_but_no_conversion(client, tmp_path):
    """変換結果は返さない。editor はこれを未変換とみなしてセットアップ画面から始める。"""
    payload = _setup_seed(
        client, tmp_path, wordlist=str(_wordlist_csv(tmp_path)), song_title="しずむ歌"
    )
    assert payload["format"] == "soramimic-editor/1"
    # ブラウザ側で導出できるものは返さない(これが「未変換」の目印になる)
    for key in ("results", "tokensList", "unitsList"):
        assert key not in payload
    # 行ごとの読みカナ。convert_project がエンジンへ渡すのと同じ前処理済みの値
    assert payload["phrases"] == ["シズム"]
    # 初期選択の単語リストは、変換込みモードと同じ組み立て(editor の conf 形式)
    assert payload["wordlist"]["filepath"] == "wordlists/words.csv"
    assert payload["wordlist"]["dbtype"] == "tidy"
    # 曲名はセットアップ画面に出すだけ
    assert payload["song"] == {"title": "しずむ歌"}


def test_seed_carries_the_lyrics_both_ways(client, tmp_path):
    """元歌詞(字幕用)はどちらのモードでもシードに載せてエディタへ渡す。

    エディタ側が元歌詞欄を持ち、編集後の生テキスト(lyrics)を返してくるための
    入口。ルビ記法は素通しする(記法はエディタの読み生成にも効かせる)。
    """
    lyrics = "｜静寂《しじま》\nしずむ"
    # 解析のみモード(セットアップ画面から始まるシード)
    seed = _setup_seed(client, tmp_path, lyrics=lyrics)
    assert seed["lyrics"] == lyrics
    # 変換込み(既定)モード
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(_wordlist_csv(tmp_path)), "lyrics": lyrics},
    )
    assert res.status_code == 200, res.text
    assert res.json()["lyrics"] == lyrics
    # 元歌詞が無ければフィールドごと出さない
    assert "lyrics" not in _setup_seed(client, tmp_path)


def test_setup_seed_param_is_the_effective_engine_default(client, tmp_path):
    """param はエンジン既定を埋めた実効値(パラメータUIが初期値を逆算できる形)。"""
    payload = _setup_seed(
        client,
        tmp_path,
        wordlist=str(_wordlist_csv(tmp_path)),
        convert_params="VOWEL_RATIO=0.5",
    )
    param = payload["param"]
    # 指定した値はそのまま(型は数値に寄せる)
    assert param["VOWEL_RATIO"] == 0.5
    # 未指定は本家Web UIの既定プリセット。editor の valuesFromParam が読むキー
    assert param["DUPLICATE"] is False
    assert param["MID_PHRASE_BREAK_PENALTY"] == 20
    assert param["WORD_NUMBER_PENALTY"] == 20
    # video 独自パラメータはエンジンparamに載せない(従来どおり)
    assert "NOTE_LENGTH_WEIGHT" not in param


def test_setup_seed_works_without_a_wordlist(client, tmp_path):
    """解析に単語リストは要らない。リストはセットアップ画面で選ぶ。

    エディタ内の自作リスト(ORIGINAL)を使っているあいだ video 側の #wordlist は
    空になるが、その状態でも⚙から開けること。entry を入れられないので
    wordlist フィールドは省き、editor は conf の既定(active)で始まる。
    """
    payload = _setup_seed(client, tmp_path)
    assert payload["phrases"] == ["シズム"]
    assert "wordlist" not in payload
    # 変換込み(既定)では従来どおり単語リスト必須のまま
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
    )
    assert res.status_code == 422


def test_setup_seed_puts_the_filter_on_the_entry(client, tmp_path):
    """絞り込み(where)は単語リストのエントリに載せる。

    ファセットを持たないリスト(自作リスト・conf に無いCSV)では、editor は
    エントリの where をそのまま使う。チェックボックスで表せる形ではないので
    トップレベルには出さない——出すと editor が「どのチェックにも当たらない」と
    解釈して絞り込みを空にしてしまう。
    """
    payload = _setup_seed(
        client, tmp_path, wordlist=str(_wordlist_csv(tmp_path)), where="type=family"
    )
    assert payload["wordlist"]["where"] == "type=family"
    assert "where" not in payload


def test_setup_seed_passes_a_restorable_filter_at_the_top_level(client, tmp_path):
    """ファセットで表せる where は、editor が復元できるようトップレベルにも載せる。

    editor はトップレベルの where からチェック状態を復元し(restoreFacets)、
    変換時にそのチェックから where を組み直す。形がそろっている今は、渡した
    条件がそのまま復元される(tests/test_facets.py が3実装の形を固定)。
    載せないと editor は conf の既定チェックで始まってしまい、既定と違う
    絞り込みで作られた替え歌が、編集ツールに入った瞬間に条件を失う。
    """
    # submodule未取得でもディレクトリだけは在る(空)ので、中身の実在で判定する
    root = Path(__file__).resolve().parents[1] / "external"
    if not (root / "soramimic-wordlists" / "baseball.csv").is_file() or not (
        root / "soramimic" / "conf" / "setting.json"
    ).is_file():
        pytest.skip("submodule未取得")
    from soramimic_video.facets import default_where

    # 既定の絞り込み(video が何も指定しないときに使う条件)
    payload = _setup_seed(client, tmp_path, wordlist="baseball")
    assert payload["wordlist"]["value"] == "BASEBALL"
    assert payload["where"] == default_where(payload["wordlist"])
    assert payload["where"] == "((type=family) or (type=full) or (type=registered))"
    assert payload["wordlist"]["where"] == payload["where"]
    # 既定と違う絞り込み(ファセットの1つだけ)もそのまま渡る
    picked = "((type=nick))"
    payload = _setup_seed(client, tmp_path, wordlist="baseball", where=picked)
    assert payload["where"] == picked
    # チェックボックスで表せない形は載せない(渡すと絞り込みが消えるため)
    payload = _setup_seed(client, tmp_path, wordlist="baseball", where="type=nick")
    assert payload["wordlist"]["where"] == "type=nick"
    assert "where" not in payload


def test_setup_seed_note_length_weights(client, tmp_path):
    """α>0 のときは変換せずに重みだけ計算して同梱する(トークナイズのみ)。

    変換しないので単語DBは要らないが、重みの計算にはエンジンと同じユニット列が
    必要なので、トークナイザだけを呼ぶ(soramimic_engine.run_tokenize)。
    """
    payload = _setup_seed(
        client,
        tmp_path,
        wordlist=str(_wordlist_csv(tmp_path)),
        convert_params="NOTE_LENGTH_WEIGHT=1",
    )
    weights = payload["weightsList"]
    raw = payload["noteLengthRawList"]
    assert payload["noteLengthAlpha"] == 1
    assert raw == weights
    assert len(raw) == len(payload["phrases"])
    for row in raw:
        assert row and all(isinstance(w, (int, float)) and w >= 0 for w in row)
    default = _setup_seed(
        client, tmp_path, wordlist=str(_wordlist_csv(tmp_path))
    )
    assert default["noteLengthAlpha"] == 0.25
    assert default["noteLengthRawList"]


def test_setup_seed_omits_note_length_fields_when_projection_is_unavailable(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        convert_mod,
        "project_note_length_weights",
        lambda project, alpha: lambda units: None,
    )
    payload = _setup_seed(client, tmp_path, wordlist=str(_wordlist_csv(tmp_path)))
    assert "noteLengthRawList" not in payload
    assert "noteLengthAlpha" not in payload
    assert "weightsList" not in payload


def test_setup_seed_accepts_a_custom_wordlist(client, tmp_path):
    """自作リスト(アップロード)も解析のみモードで初期選択にできる。"""
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files=[("midi", ("song.mid", midi.read_bytes(), "audio/midi"))],
        data={"wordlist_text": CUSTOM_TEXT, "convert": "0"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    entry = payload["wordlist"]
    sid = entry["value"].removeprefix("custom:")
    assert entry["filepath"] == f"session-wordlists/{sid}.csv"
    assert "results" not in payload
    # DB構築用の正規化CSVは変換込みモードと同じく置かれる
    assert (tmp_path / "jobs" / "editor-sessions" / sid / "wordlist.csv").is_file()


def test_editor_session_still_converts_by_default(client, tmp_path):
    """convert を送らなければ従来どおり変換済みJSON(後方互換)。"""
    midi = _xf_midi(tmp_path)
    res = client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(_wordlist_csv(tmp_path))},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["results"] and payload["unitsList"] and payload["tokensList"]


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


def test_job_pipeline_keeps_the_align_lines_originals(job_client, tmp_path, monkeypatch):
    """字幕の元歌詞は analyze 段の align_lines が正。editor の originalLines は使わない。

    ブラウザ側の対応づけは精度が足りず字幕が劣化するので採用しない。元歌詞が
    フォームから来ていないジョブだけ、editor.json の lyrics(生テキスト)を
    align_lines にかけて補う。動画に渡る project で確かめる。
    """
    from soramimic_video import mix as mix_mod
    from soramimic_video import video as video_mod

    midi = _xf_midi(tmp_path)
    session = job_client.post(
        "/api/editor-session",
        files={"midi": ("song.mid", midi.read_bytes(), "audio/midi")},
        data={"wordlist": str(_wordlist_csv(tmp_path))},
    ).json()
    # 取り込み側が引ける実在のCSVにしておく(単語リスト解決はこのテストの主題ではない)
    session["wordlist"]["filepath"] = str(_wordlist_csv(tmp_path))
    session["originalLines"] = ["エディタが対応づけた元歌詞"]
    session["lyrics"] = "エディタの元歌詞"

    res = job_client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", midi.read_bytes(), "audio/midi"),
            "editor": ("editor.json", json.dumps(session).encode(), "application/json"),
        },
        data={"lyrics": "しずむ"},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    params = job_client.get(f"/api/jobs/{job_id}").json()["params"]
    job = api_mod.Job(id=job_id, dir=tmp_path / "jobs" / job_id, params=params)

    out = tmp_path / "out.mp4"
    seen: dict = {}

    def fake_make_video(project, d, **kwargs):
        seen["originals"] = [ln.original_text for ln in project.lines]
        seen.setdefault("image_leads", []).append(kwargs["image_lead_sec"])
        return out

    monkeypatch.setattr(api_mod, "_run_synthesize", lambda *a, **k: None)
    monkeypatch.setattr(mix_mod, "mix", lambda *a, **k: None)
    monkeypatch.setattr(video_mod, "make_video", fake_make_video)

    # フォームの元歌詞(lyrics.txt)を align_lines にかけた結果がそのまま残る
    assert REAL_RUN_PIPELINE(job, {"parallel_video": False}) == out
    assert seen["originals"] == ["しずむ"]
    assert seen["image_leads"] == [0.1]

    # 元歌詞をフォームから送っていないジョブは、editor.json の lyrics で埋まる
    (job.dir / "lyrics.txt").unlink()
    session["lyrics"] = "しずむ夜"
    (job.dir / "editor.json").write_text(json.dumps(session), encoding="utf-8")
    assert REAL_RUN_PIPELINE(
        job, {"video_image_lead_sec": 0, "parallel_video": False}
    ) == out
    assert seen["originals"] == ["しずむ夜"]
    assert seen["image_leads"][-1] == 0


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


# ---- エディタの中で選んだ自作リスト(wordlist.value = ORIGINAL + csvText)----
# エディタ側の⚙モーダルで自作リストを使うと、書き出しJSONに単語データそのもの
# (csvText)が入る。サーバーのセッション置き場を必要としない自己完結の経路。

# idは連番でない(=取り込み側が振り直したら行が当たらなくなる)
ORIGINAL_CSV = (
    "id,original,surface,pronunciation\n"
    "7,静岡駅,静岡,シズオカ\n"
    "42,鈴鹿,鈴鹿,スズカ"
)

# 既知/未知どちらの単語もsurfaceで必ず出るレイアウト(プレビューのキューが消えない)
_SHOW_ALL_LAYOUT = {
    "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2]}],
    "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2]}],
}


def _original_entry(csv_text: str | None = ORIGINAL_CSV) -> dict:
    entry = {"value": "ORIGINAL", "text": "自作リスト"}
    if csv_text is not None:
        entry["csvText"] = csv_text
    return entry


def _original_payload(client, tmp_path, csv_text: str | None = ORIGINAL_CSV) -> dict:
    """エディタの中で自作リストに切り替えて書き出したJSON相当。

    変換結果(results/unitsList)は実際のセッションから作り、単語リストだけを
    ORIGINAL+csvText に差し替える(先頭単語は csvText の id=42 の行を指す)。
    """
    payload = _custom_session(client, tmp_path)
    payload["wordlist"] = _original_entry(csv_text)
    payload["results"][0][0] = dict(
        payload["results"][0][0],
        surface="鈴鹿", kana="スズカ", original="鈴鹿", id="42",
        pronunciation=["ス", "ズ", "カ"],
    )
    return payload


def _import(payload: dict, tmp_path: Path):
    """editor.json を書いて import_editor に通す(セッション置き場は渡さない)。"""
    from soramimic_video.editor_io import import_editor
    from soramimic_video.xfparse import analyze_midi

    d = tmp_path / "project"
    d.mkdir(exist_ok=True)
    (d / "editor.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    project = analyze_midi(_xf_midi(tmp_path))
    import_editor(project, d, d / "editor.json")
    return project, d


def test_import_editor_uses_embedded_original_csv(client, tmp_path):
    """csvTextの行がそのまま引ける(idは振り直さない)。セッションは不要。"""
    project, d = _import(_original_payload(client, tmp_path), tmp_path)
    row = project.parody.lines[0].words[0].wordlist_row
    assert row is not None
    assert (row["id"], row["surface"], row["original"]) == ("42", "鈴鹿", "鈴鹿")
    # 単語データはジョブディレクトリに置かれ、以後は普通のCSVとして扱える
    saved = d / "original-wordlist.csv"
    assert str(saved) == project.parody.wordlist
    assert saved.read_text(encoding="utf-8").startswith("id,original,surface,pronunciation\n")
    assert not saved.read_text(encoding="utf-8").endswith("\n")  # 末尾改行なし
    assert project.parody.where is None  # 自作リストに絞り込みは無い


def test_import_editor_original_requires_csv_text(client, tmp_path):
    payload = _original_payload(client, tmp_path, csv_text=None)
    with pytest.raises(ValueError, match="csvText"):
        _import(payload, tmp_path)
    # 文字列でない値(オブジェクトなど)も同じ扱い
    payload["wordlist"] = {"value": "ORIGINAL", "csvText": {"rows": []}}
    with pytest.raises(ValueError, match="csvText"):
        _import(payload, tmp_path)


def test_import_editor_original_rejects_broken_csv(client, tmp_path):
    """ヘッダが無い・必要な列が無いCSVは、黙って別のリストに落ちずにエラーにする。"""
    for broken, message in (
        ("静岡,シズオカ\n鈴鹿,スズカ", "surface"),          # ヘッダ行が無い
        ("surface,pronunciation\n静岡,シズオカ", "id"),      # id列が無い
        ("", "csvText"),                                    # 空
    ):
        payload = _original_payload(client, tmp_path, csv_text=broken)
        with pytest.raises(ValueError, match=message):
            _import(payload, tmp_path)


def test_import_editor_original_drops_image_column(client, tmp_path):
    """image列に何が来てもサーバーのファイルは読まない(列ごと落とす)。"""
    csv_text = (
        "id,original,surface,pronunciation,image\n"
        "7,静岡駅,静岡,シズオカ,file:///etc/passwd\n"
        "42,鈴鹿,鈴鹿,スズカ,/etc/shadow"
    )
    project, d = _import(_original_payload(client, tmp_path, csv_text), tmp_path)
    row = project.parody.lines[0].words[0].wordlist_row
    assert row is not None and row["surface"] == "鈴鹿"
    assert "image" not in row  # 動画側(download_image)へ渡る値そのものが無い
    saved = (d / "original-wordlist.csv").read_text(encoding="utf-8")
    assert "passwd" not in saved and "shadow" not in saved and "file://" not in saved


def _preview_payload(csv_text: str = ORIGINAL_CSV) -> dict:
    return {
        "format": "soramimic-editor/1",
        "phrases": ["シズオカ"],
        "results": [[{
            "surface": "鈴鹿", "kana": "スズカ", "id": "42", "original": "鈴鹿",
            "originalkana": "シズオカ", "original_surface": "シズオカ",
        }]],
        "unitsList": [[]],
        "wordlist": _original_entry(csv_text),
    }


def test_editor_preview_resolves_embedded_original_csv(client):
    """プレビューでも csvText の行が引ける(フォームのリスト名には落ちない)。"""
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json",
                          json.dumps(_preview_payload()).encode(), "application/json")},
        data={"layout_json": json.dumps(_SHOW_ALL_LAYOUT), "wordlist": "stations"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    assert body["use_fallback"] is False          # 単語リストの行が当たっている
    assert body["data"]["pronunciation"] == "スズカ"  # 行から来る列(単語側には無い)
    assert body["wordlist"] == ""                 # 名前で引けるリストではない


def test_editor_preview_original_keeps_shared_ids(client):
    """1語に読みが複数ある行(同じidが並ぶ)でも、表記で正しい行が当たる。

    エディタの plainToCsv は「読みが複数の語」に同じidの行を並べる(0始まり)。
    idを振り直すとこの対応が崩れるので、そのままの並びで引けることを見る。
    """
    csv_text = (
        "id,original,surface,pronunciation\n"
        "0,カレーライス,カレー,カレー\n"
        "0,カレーライス,ライス,ライス\n"
        "1,寿司,寿司,スシ"
    )
    payload = _preview_payload(csv_text)
    payload["results"][0][0] = dict(
        payload["results"][0][0], surface="ライス", kana="ライス", id="0"
    )
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json",
                          json.dumps(payload).encode(), "application/json")},
        data={"layout_json": json.dumps(_SHOW_ALL_LAYOUT)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["use_fallback"] is False
    assert body["data"]["pronunciation"] == "ライス"  # カレー(同じid)の行ではない


def test_editor_preview_original_ignores_image_column(client):
    csv_text = (
        "id,original,surface,pronunciation,image\n"
        "42,鈴鹿,鈴鹿,スズカ,file:///etc/passwd"
    )
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json",
                          json.dumps(_preview_payload(csv_text)).encode(),
                          "application/json")},
        data={"layout_json": json.dumps(_SHOW_ALL_LAYOUT)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["image_url"] == "" and "image" not in body["data"]


def test_editor_preview_original_reports_broken_csv(client):
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json",
                          json.dumps(_preview_payload("静岡,シズオカ")).encode(),
                          "application/json")},
        data={"layout_json": json.dumps(_SHOW_ALL_LAYOUT)},
    )
    assert res.status_code == 400
    assert "自作リスト" in res.json()["detail"]


def _post_job(job_client, payload: dict):
    return job_client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "editor": ("editor.json", json.dumps(payload).encode(), "application/json"),
        },
    )


def test_job_accepts_editor_with_embedded_original_csv(job_client, tmp_path):
    """csvText付きのeditor JSONは、リストを再添付しなくても投入できる。"""
    res = _post_job(job_client, _original_payload(job_client, tmp_path))
    assert res.status_code == 200, res.text
    params = job_client.get(f"/api/jobs/{res.json()['id']}").json()["params"]
    assert params["wordlist"] == "自作リスト"  # 履歴・ダウンロード名に出る表示名
    assert params["where"] == ""
    assert params["parody_source"] == "editor"


def test_job_rejects_original_without_csv_text(job_client):
    res = _post_job(job_client, {
        "results": [], "unitsList": [], "wordlist": _original_entry(csv_text=None),
    })
    assert res.status_code == 422
    assert "csvText" in res.json()["detail"]


def test_job_rejects_broken_original_csv(job_client):
    res = _post_job(job_client, {
        "results": [], "unitsList": [],
        "wordlist": _original_entry("静岡,シズオカ\n鈴鹿,スズカ"),
    })
    assert res.status_code == 400
    assert "surface" in res.json()["detail"]


def test_job_rejects_oversized_original_csv(job_client, monkeypatch):
    monkeypatch.setenv("SORAMIMIC_MAX_WORDLIST_BYTES", "50")
    res = _post_job(job_client, {
        "results": [], "unitsList": [], "wordlist": _original_entry(),
    })
    assert res.status_code == 413
    assert "大きすぎます" in res.json()["detail"]


# ---- filler(万能候補)----
# 変換エンジンは単語が足りない/どの単語も合わない1ユニット区間を「元歌詞の
# かなのまま」の仮想語(filler)で埋める(soramimic#128)。この単語は単語リストの
# 語ではないので id を持たない。取り込みでもプレビューでも、行が引けない単語
# =文字フレームのフォールバックとして素直に流れることを見る。


def _filler_words(units: list[dict]) -> list[dict]:
    """ユニット列を全部 filler で埋めた results 1行ぶん(エンジンの出力と同じ形)。"""
    return [
        {
            "surface": u["pronunciation"],
            "pronunciation": u["pronunciation"],
            "kana": u["pronunciation"],
            "original": "",
            "filler": True,
            "sim": 1e6,
            "score": 1e6,
            "originalkana": u["pronunciation"],
            "original_surface": u["pronunciation"],
            "period": [i, i + 1],
        }
        for i, u in enumerate(units)
    ]


def test_import_editor_accepts_filler_words(client, tmp_path):
    """id を持たない filler が混ざっても取り込めて、行は引かない(文字フレーム行き)。"""
    payload = _original_payload(client, tmp_path)
    payload["results"][0] = _filler_words(payload["unitsList"][0])
    project, _d = _import(payload, tmp_path)

    words = project.parody.lines[0].words
    assert words, "filler だけの行が空になっている"
    assert all(w.filler for w in words)
    # 単語リストの語ではないので行は引かない(=単語画像なし・文字フレーム)
    assert all(w.wordlist_row is None for w in words)
    # 歌わせるカナは元歌詞のまま。音符にも対応づく
    assert all(w.note_ids for w in words)
    assert "".join(w.kana for w in words) == "".join(
        u["pronunciation"] for u in payload["unitsList"][0]
    )


def test_import_editor_filler_survives_project_roundtrip(client, tmp_path):
    """filler フラグは project.json を書いて読み直しても残る(旧project.jsonも読める)。"""
    from soramimic_video.project import Project

    payload = _original_payload(client, tmp_path)
    payload["results"][0] = _filler_words(payload["unitsList"][0])
    project, d = _import(payload, tmp_path)
    project.save(d)
    assert all(w.filler for w in Project.load(d).parody.lines[0].words)

    # filler キーが無い旧 project.json も既定 False で読める
    raw = json.loads((d / "project.json").read_text(encoding="utf-8"))
    for pl in raw["parody"]["lines"]:
        for w in pl["words"]:
            del w["filler"]
    (d / "project.json").write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    assert not any(w.filler for w in Project.load(d).parody.lines[0].words)


def test_editor_preview_filler_uses_fallback(client):
    """レイアウトプレビューでも filler は行なし=フォールバック側で描く。"""
    payload = _preview_payload()
    payload["results"][0] = [
        dict(_filler_words([{"pronunciation": "シズオカ"}])[0], period=[0, 1])
    ]
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json",
                          json.dumps(payload).encode(), "application/json")},
        data={"layout_json": json.dumps(_SHOW_ALL_LAYOUT)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1                    # キューから消えない
    assert body["use_fallback"] is True          # 単語リストの行が当たっていない
    assert body["data"]["surface"] == "シズオカ"   # 元歌詞のかながそのまま出る
    assert not body["data"].get("image")         # 単語画像は無い


def test_job_accepts_editor_with_filler(job_client, tmp_path):
    """filler 混じりの editor.json はそのままジョブに投入できる。"""
    payload = _original_payload(job_client, tmp_path)
    payload["results"][0] = _filler_words(payload["unitsList"][0])
    res = job_client.post(
        "/api/jobs",
        files={
            "midi": ("song.mid", FAKE_MIDI, "audio/midi"),
            "editor": ("editor.json", json.dumps(payload).encode(), "application/json"),
        },
    )
    assert res.status_code == 200, res.text


def test_index_treats_original_wordlist_as_custom():
    """トップ画面の表示でも ORIGINAL は custom: と同じ「自作リスト」扱いにする。"""
    index = (
        Path(__file__).resolve().parents[1] / "src/soramimic_video/static/index.html"
    ).read_text(encoding="utf-8")
    body = index[index.index("async function updateParodyStatus(") :]
    body = body[: body.index("\n}\n")]
    # filepath を持たない ORIGINAL も拾い、名前は w.text(=自作リスト)を使う
    assert '=== "ORIGINAL"' in body
    assert 'w.text || "自作リスト"' in body
    # 編集内容の指紋は results を見るので、エディタ内でリストだけ切り替えて
    # 変換結果が変われば「編集あり」として検知される(=生成に反映される)
    sig = index[index.index("function editorContentSig(") :]
    assert "data.results" in sig[: sig.index("\n}\n")]


def test_setting_json_from_source_conf(client):
    """配信用confはソースを正本とし、植物を含む公開候補を返す。"""
    from soramimic_video.editor_io import SETTING_JSON

    if not SETTING_JSON.is_file():
        pytest.skip("external/soramimic のsubmoduleが無い環境")

    res = client.get("/editor/conf/setting.json")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    source = json.loads(SETTING_JSON.read_text(encoding="utf-8"))
    served = res.json()

    def names(items):
        found = []
        for item in items:
            if "items" in item:
                found.extend(names(item["items"]))
            elif item.get("filepath"):
                found.append(Path(item["filepath"]).stem)
        return found

    assert "plant" in names(source["wordlist"])
    assert names(served["wordlist"]) == names(source["wordlist"])
    assert "plant" in names(served["wordlist"])
    assert json.loads(SETTING_JSON.read_text(encoding="utf-8")) == source


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
