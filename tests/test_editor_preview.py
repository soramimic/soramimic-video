"""editor書き出しJSONに基づくレイアウト編集プレビュー(/api/editor-preview)のテスト。

- キューのページ送りの境界(前後端のクランプ)
- 単語リストに行がない単語は use_fallback=True になる
- 表示できる/できないのフィルタが build_image_cues と一致する
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import build_xf_midi
from soramimic_video.editor_io import build_editor_preview
from soramimic_video.layout import load_layout
from soramimic_video.project import Parody, ParodyLine, ParodyWord
from soramimic_video.video import build_image_cues
from soramimic_video.xfparse import analyze_midi

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs"))


def _wordlist(tmp_path: Path) -> Path:
    csv_path = tmp_path / "wl.csv"
    csv_path.write_text(
        "id,original,surface,death\n"
        "10,織田信長,信長,1582\n"
        "11,豊臣秀吉,秀吉,\n",  # deathが空: require:deathの要素は出ない
        encoding="utf-8",
    )
    return csv_path


def _editor_payload(csv_path: Path) -> dict:
    """3単語(既知2 + 未知1)・2行のeditor書き出しJSON相当。"""
    return {
        "format": "soramimic-editor/1",
        "phrases": ["ノブナガヒデヨシ", "カクウノタンゴ"],
        "results": [
            [
                {"surface": "信長", "kana": "ノブナガ", "id": "10",
                 "original": "織田信長", "originalkana": "オダ", "original_surface": "オダ"},
                {"surface": "秀吉", "kana": "ヒデヨシ", "id": "11", "original": "豊臣秀吉",
                 "originalkana": "トヨトミ", "original_surface": "トヨトミ"},
            ],
            [
                {"surface": "架空", "kana": "カクウ", "id": "999",
                 "original": "", "originalkana": "カクウ", "original_surface": "カクウ"},
            ],
        ],
        "wordlist": {"filepath": str(csv_path)},
    }


# 既知/未知いずれの単語もsurfaceで必ず表示できるレイアウト(3キュー全部出る)
_LAYOUT_SHOW_ALL = {
    "elements": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2]}],
    "fallback": [{"type": "text", "text": "{surface}", "box": [0.1, 0.3, 0.8, 0.2]}],
}


def _post(client: TestClient, payload: dict, **data):
    return client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json", json.dumps(payload).encode(), "application/json")},
        data=data,
    )


def test_paging_bounds(client, tmp_path):
    payload = _editor_payload(_wordlist(tmp_path))
    layout_json = json.dumps(_LAYOUT_SHOW_ALL)

    # 3単語すべて表示 → 3キュー。歌唱順(行順・単語順)に並ぶ
    first = _post(client, payload, cue="0", layout_json=layout_json).json()
    assert first["total"] == 3
    assert first["index"] == 0
    assert first["data"]["surface"] == "信長"
    # 行の字幕(替え歌=surfaceを2スペース連結 / 元歌詞=phrase)
    assert first["parody_text"] == "信長  秀吉"
    assert first["original_text"] == "ノブナガヒデヨシ"

    # 末尾を超える指定は最終キューにクランプ
    last = _post(client, payload, cue="99", layout_json=layout_json).json()
    assert last["index"] == 2
    assert last["data"]["surface"] == "架空"

    # 負のインデックスは先頭にクランプ
    neg = _post(client, payload, cue="-5", layout_json=layout_json).json()
    assert neg["index"] == 0


def test_unknown_word_uses_fallback(client, tmp_path):
    payload = _editor_payload(_wordlist(tmp_path))
    layout_json = json.dumps(_LAYOUT_SHOW_ALL)

    # 単語リストに行がある単語(信長)は use_fallback=False、画像URLは付かない(image列なし)
    known = _post(client, payload, cue="0", layout_json=layout_json).json()
    assert known["use_fallback"] is False

    # 単語リストに行がない単語(架空 id=999)は use_fallback=True
    unknown = _post(client, payload, cue="2", layout_json=layout_json).json()
    assert unknown["use_fallback"] is True
    assert unknown["image_url"] == ""
    # 行がないので列参照(death等)は入らない
    assert "death" not in unknown["data"]


def test_lyrics_align_to_original_text(client, tmp_path):
    """元歌詞を渡すと、字幕の元歌詞がカナ(phrases)ではなく対応する元歌詞行になる。"""
    payload = _editor_payload(_wordlist(tmp_path))
    layout_json = json.dumps(_LAYOUT_SHOW_ALL)
    lyrics = "のぶなが秀吉\nかくうの単語"

    first = _post(client, payload, cue="0", layout_json=layout_json, lyrics=lyrics).json()
    assert first["original_text"] == "のぶなが秀吉"
    last = _post(client, payload, cue="2", layout_json=layout_json, lyrics=lyrics).json()
    assert last["original_text"] == "かくうの単語"

    # 元歌詞に対応する行がなければ従来どおりカナ(phrase)にフォールバック
    unmatched = _post(
        client, payload, cue="0", layout_json=layout_json, lyrics="全然違う歌詞"
    ).json()
    assert unmatched["original_text"] == "ノブナガヒデヨシ"


def _granularity_payload(csv_path: Path) -> dict:
    """2フレーズ(=2行・各1単語)が1つの元歌詞行に対応する粒度テスト用ペイロード。"""
    return {
        "format": "soramimic-editor/1",
        "phrases": ["沈むように", "溶けるように"],  # align は表記の重なりで対応づく
        "results": [
            [{"surface": "静", "kana": "シズ", "id": "10", "original": "",
              "originalkana": "", "original_surface": ""}],
            [{"surface": "川", "kana": "カワ", "id": "11", "original": "",
              "originalkana": "", "original_surface": ""}],
        ],
        "wordlist": {"filepath": str(csv_path)},
    }


def test_granularity_original_line_vs_phrase(client, tmp_path):
    """元歌詞の粒度: 既定(line)は行全文、phrase はフレーズの部分文字列。"""
    payload = _granularity_payload(_wordlist(tmp_path))
    layout_json = json.dumps(_LAYOUT_SHOW_ALL)
    lyrics = "沈むように 溶けるように"

    # 既定(未指定=original:line): 両フレーズとも行全文
    for cue in ("0", "1"):
        body = _post(client, payload, cue=cue, layout_json=layout_json, lyrics=lyrics).json()
        assert body["original_text"] == "沈むように 溶けるように"

    # original:phrase: フレーズごとに部分文字列へ切り分ける
    g = "parody:phrase|original:phrase"
    first = _post(client, payload, cue="0", layout_json=layout_json, lyrics=lyrics,
                  subtitle_granularity=g).json()
    second = _post(client, payload, cue="1", layout_json=layout_json, lyrics=lyrics,
                   subtitle_granularity=g).json()
    assert first["original_text"] == "沈むように"
    assert second["original_text"] == "溶けるように"


def test_granularity_parody_line_concatenates(client, tmp_path):
    """替え歌の粒度 line: 同じ元歌詞行の替え歌を連結して両フレーズに出す。"""
    payload = _granularity_payload(_wordlist(tmp_path))
    layout_json = json.dumps(_LAYOUT_SHOW_ALL)
    lyrics = "沈むように 溶けるように"
    g = "parody:line|original:line"
    for cue in ("0", "1"):
        body = _post(client, payload, cue=cue, layout_json=layout_json, lyrics=lyrics,
                     subtitle_granularity=g).json()
        assert body["parody_text"] == "静  川"


def test_preview_original_matches_video(tmp_path):
    """editor-preview の元歌詞テキストが video.build_ass と一致する(共通ロジック)。"""
    from helpers import build_xf_midi
    from soramimic_video.align import align_lines
    from soramimic_video.editor_io import build_editor_preview
    from soramimic_video.layout import DEFAULT_SUBTITLES, Layout
    from soramimic_video.project import Parody, ParodyLine, ParodyWord
    from soramimic_video.video import build_ass
    from soramimic_video.xfparse import analyze_midi

    midi = build_xf_midi(
        tmp_path / "m.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64), (1200, 240, 65)],
        lyric_events=[(480, "沈む"), (720, "ように"), (960, "/溶ける"), (1200, "ように")],
    )
    project = analyze_midi(midi)
    align_lines(project, ["沈むように 溶けるように"])
    project.parody = Parody(wordlist="t", lines=[
        ParodyLine(line_id=project.lines[0].id, words=[ParodyWord(
            surface="静", kana="シズ", original="", original_surface="", originalkana="",
            note_ids=[0, 1])]),
        ParodyLine(line_id=project.lines[1].id, words=[ParodyWord(
            surface="川", kana="カワ", original="", original_surface="", originalkana="",
            note_ids=[2, 3])]),
    ])
    payload = _granularity_payload(tmp_path / "wl.csv")  # phrases が project の XF行と同じ切り

    override = {"parody": "phrase", "original": "phrase"}
    ass = build_ass(project, 1280, 720, "Font", None, override)
    video_orig = [ln.split(",,")[-1].split("}")[-1]
                  for ln in ass.splitlines()
                  if ln.startswith("Dialogue:") and ",Original," in ln]

    layout = Layout(elements=[], subtitles=list(DEFAULT_SUBTITLES))
    preview = build_editor_preview(payload, None, layout, "沈むように 溶けるように", override)
    preview_orig = [c["original_text"] for c in preview["cues"]]

    assert preview_orig == video_orig == ["沈むように", "溶けるように"]


def test_no_editor_words_shows_zero(client, tmp_path):
    # 画像だけのレイアウトでは、画像列のない単語は表示できず0キューになる
    payload = _editor_payload(_wordlist(tmp_path))
    layout_json = json.dumps({"elements": [{"type": "image", "box": [0, 0, 1, 1]}]})
    body = _post(client, payload, cue="0", layout_json=layout_json).json()
    assert body == {"total": 0, "index": 0, "wordlist": str(_wordlist(tmp_path))}


def test_rejects_bad_editor_json(client):
    res = client.post(
        "/api/editor-preview",
        files={"editor": ("editor.json", b"{oops", "application/json")},
    )
    assert res.status_code == 400


def _hand_project(tmp_path: Path, csv_path: Path):
    """CSVの行と同じ内容の wordlist_row を持つ手組みプロジェクト(音符付き)。

    build_image_cues と build_editor_preview を同じ単語データで突き合わせるため、
    editor payload の results と1対1に対応させる。
    """
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64)],
        lyric_events=[(480, "の"), (720, "ひ"), (960, "か")],
    )
    project = analyze_midi(midi)
    project.parody = Parody(
        wordlist=str(csv_path),
        lines=[
            ParodyLine(line_id=0, words=[
                ParodyWord(surface="信長", kana="ノブナガ", original="織田信長",
                           original_surface="オダ", originalkana="オダ", note_ids=[0],
                           wordlist_row={"id": "10", "original": "織田信長",
                                         "surface": "信長", "death": "1582"}),
                ParodyWord(surface="秀吉", kana="ヒデヨシ", original="豊臣秀吉",
                           original_surface="トヨトミ", originalkana="トヨトミ", note_ids=[1],
                           wordlist_row={"id": "11", "original": "豊臣秀吉",
                                         "surface": "秀吉", "death": ""}),
            ]),
            ParodyLine(line_id=1, words=[
                ParodyWord(surface="架空", kana="カクウ", original="",
                           original_surface="カクウ", originalkana="カクウ", note_ids=[2],
                           wordlist_row=None),  # 未知語
            ]),
        ],
    )
    return project


@pytest.mark.parametrize(
    "layout_spec",
    [
        # 全単語がsurfaceで表示できる → 3キュー
        _LAYOUT_SHOW_ALL,
        # 没年(death)がある単語だけ表示(require)。画像も他要素もなし。
        # → 信長(1582)だけ表示、秀吉(death空)と未知語はスキップ = 1キュー
        {"elements": [{"type": "text", "text": "没年 {death}",
                       "box": [0.1, 0.3, 0.8, 0.2], "require": "death"}]},
    ],
)
def test_filtering_matches_build_image_cues(tmp_path, layout_spec):
    csv_path = _wordlist(tmp_path)
    layout = load_layout(str(_write(tmp_path / "lay.json", layout_spec)))

    project = _hand_project(tmp_path, csv_path)
    cues, _ = build_image_cues(project, tmp_path / "video", 320, 180, layout=layout)

    preview = build_editor_preview(_editor_payload(csv_path), None, layout)

    # 表示できる/できないのフィルタが一致(=キュー枚数が動画と揃う)
    assert len(preview["cues"]) == len(cues)


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path
