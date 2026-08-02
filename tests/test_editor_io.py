import json
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.convert import convert_project
from soramimic_video.editor_io import (
    SETTING_JSON,
    export_editor,
    import_editor,
    save_raw,
    wordlist_display_name,
)
from soramimic_video.xfparse import analyze_midi


def _project(tmp_path: Path):
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64)],
        lyric_events=[(0, "し"), (240, "ず"), (480, "む")],
    )
    return analyze_midi(midi)


def _wordlist(tmp_path: Path) -> Path:
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n"
        "0,静岡駅,静岡,シズオカ\n"
        "1,鈴鹿,鈴鹿,スズカ\n"
        "2,清水,清水,シミズ",
        encoding="utf-8",
    )
    return csv_path


def test_editor_roundtrip(tmp_path: Path):
    project = _project(tmp_path)
    raw = convert_project(project, wordlist=str(_wordlist(tmp_path)))
    save_raw(raw, tmp_path)

    path = export_editor(project, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "soramimic-editor/1"
    assert len(payload["results"]) == len(project.lines)
    assert len(payload["unitsList"]) == len(project.lines)
    assert payload["tokensList"], "tokensList(editorの再生成に必要)が空"
    assert payload["wordlist"]["dbtype"] == "tidy"

    # editorでの編集をシミュレート: 先頭単語を別候補(清水)に差し替えて固定
    word = payload["results"][0][0]
    edited = dict(
        word,
        surface="清水", kana="シミズ", original="清水", id="2",
        pronunciation=["シ", "ミ", "ズ"], locked=True,
    )
    payload["results"][0][0] = edited
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    import_editor(project, tmp_path)
    words = project.parody.lines[0].words
    assert words[0].surface == "清水"
    assert words[0].locked is True
    assert words[0].note_ids, "音符への対応づけが失われた"
    assert words[0].wordlist_row is not None
    assert words[0].wordlist_row["original"] == "清水"

    # 取り込み後も再書き出しできる(生応答が更新されている)
    path2 = export_editor(project, tmp_path)
    payload2 = json.loads(path2.read_text(encoding="utf-8"))
    assert payload2["results"][0][0]["surface"] == "清水"
    assert payload2["results"][0][0]["locked"] is True


def test_import_editor_without_convert(tmp_path: Path):
    # ブラウザで変換・編集したJSONだけを、convertを経ていないプロジェクトに取り込む
    project = _project(tmp_path)
    donor = _project(tmp_path)
    raw = convert_project(donor, wordlist=str(_wordlist(tmp_path)))
    save_raw(raw, tmp_path)
    path = export_editor(donor, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 単語リストの解決に使うfilepathを、実在するCSVパスに差し替えておく
    payload["wordlist"]["filepath"] = str(_wordlist(tmp_path))
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert project.parody is None
    import_editor(project, tmp_path)
    assert project.parody is not None
    assert project.parody.lines[0].words, "取り込んだ単語が空"
    assert project.parody.lines[0].words[0].note_ids


_FACET_SETTING = {
    "wordlist": [
        {
            "value": "WORDS",
            "text": "テスト",
            "filepath": "wordlists/words.csv",
            "dbtype": "tidy",
            "facets": [
                {
                    "column": "type",
                    "label": "種類",
                    "values": [
                        {"v": "family", "label": "名字", "default": True},
                        {"v": "nick", "label": "愛称"},
                    ],
                }
            ],
        }
    ]
}


def _facet_wordlist(tmp_path: Path) -> Path:
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation,type\n"
        "0,静岡駅,静岡,シズオカ,family\n"
        "1,鈴鹿,鈴鹿,スズカ,family\n"
        "2,清水,清水,シミズ,nick",
        encoding="utf-8",
    )
    return csv_path


def test_editor_json_carries_the_facet_filter_both_ways(tmp_path: Path, monkeypatch):
    """ファセットで表せる絞り込みは editor JSON のトップレベルにも載せ、読み戻す。

    editor は⚙モーダルのチェック状態をトップレベルの where から復元し
    (restoreFacets)、絞り込みを操作すると wordlist を conf のエントリ
    (where なし)で置き換えて条件はトップレベルだけに持つ。だから
    - 書き出し: 載せないと editor の再変換が conf の既定に戻ってしまう
    - 取り込み: トップレベルを先に見ないと、ユーザーが変えた条件が落ちる
    """
    import soramimic_video.editor_io as editor_io

    setting = tmp_path / "setting.json"
    setting.write_text(json.dumps(_FACET_SETTING, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(editor_io, "SETTING_JSON", setting)

    project = _project(tmp_path)
    csv_path = _facet_wordlist(tmp_path)
    where = "((type=family))"
    raw = convert_project(project, wordlist=str(csv_path), where=where)
    save_raw(raw, tmp_path)

    payload = json.loads(export_editor(project, tmp_path).read_text(encoding="utf-8"))
    assert payload["where"] == where
    assert payload["wordlist"]["where"] == where

    # editorで絞り込みを変えた状態(conf のエントリ + トップレベルの where)
    payload["wordlist"] = dict(_FACET_SETTING["wordlist"][0], filepath=str(csv_path))
    payload["where"] = "((type=nick))"
    (tmp_path / "editor.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    import_editor(project, tmp_path)
    assert project.parody.where == "((type=nick))"

    # 旧JSON(トップレベルなし)はエントリの where を使う(後方互換)
    del payload["where"]
    payload["wordlist"]["where"] = where
    (tmp_path / "editor.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    import_editor(project, tmp_path)
    assert project.parody.where == where


# ---- 単語リストの表示名(conf/setting.json) ----

_SETTING = {
    "wordlist": [
        {"value": "STATION", "text": "駅名", "filepath": "wordlists/stations.csv"},
        # editorの選択肢は見出し(label)でグループ化されることがある
        {"label": "生物", "items": [
            {"value": "SEKITSUI", "text": "動物", "filepath": "wordlists/sekitsui.csv"},
            {"value": "PLANT", "text": "植物"},  # filepathが無ければvalueで引く
        ]},
    ]
}


def test_wordlist_display_name_from_group(tmp_path: Path, monkeypatch):
    import soramimic_video.editor_io as editor_io

    setting = tmp_path / "setting.json"
    setting.write_text(json.dumps(_SETTING, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(editor_io, "SETTING_JSON", setting)

    assert editor_io.wordlist_display_name("stations") == "駅名"
    assert editor_io.wordlist_display_name("sekitsui") == "動物"  # グループ内のリスト
    assert editor_io.wordlist_display_name("plant") == "植物"  # value(大小無視)で照合
    assert editor_io.wordlist_display_name("unknown") == "unknown"  # 設定に無ければstem
    assert editor_io.wordlist_display_name("") == ""


def test_wordlist_display_name_without_setting(tmp_path: Path, monkeypatch):
    import soramimic_video.editor_io as editor_io

    monkeypatch.setattr(editor_io, "SETTING_JSON", tmp_path / "missing.json")
    assert editor_io.wordlist_display_name("stations") == "stations"


@pytest.mark.skipif(not SETTING_JSON.is_file(), reason="submoduleのconfが無い")
@pytest.mark.parametrize(
    ("stem", "text"),
    [
        ("sekitsui", "動物"),
        ("baseball", "野球選手"),
        ("stations", "駅名"),
        ("fictional_anime_character", "ファンタジー"),
    ],
)
def test_wordlist_display_name_real_setting(stem: str, text: str):
    assert wordlist_display_name(stem) == text
