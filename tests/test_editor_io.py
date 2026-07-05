import json
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.convert import BRIDGE_DIR, convert_project
from soramimic_video.editor_io import export_editor, import_editor, save_raw
from soramimic_video.xfparse import analyze_midi

pytestmark = pytest.mark.skipif(
    not (BRIDGE_DIR / "node_modules").exists(),
    reason="bridge未セットアップ(cd bridge && npm ci)",
)


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
