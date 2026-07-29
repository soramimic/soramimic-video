"""青空文庫ルビ記法(｜表層《よみ》)の統合テスト。

エンジン(soramimic)側のパーサ・強制トークンは本家のテストに任せ、ここでは
video 側の約束
  1. 表示(字幕・元歌詞)・アライメントには素テキストを使う(記法が漏れない)
  2. 読み(reading.py 経由)には注釈の読みが効く
  3. 記法を含まない入力は完全に従来どおり
を確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mido import Message, MidiFile, MidiTrack

from soramimic_video.align import align_lines, align_texts, split_lyric_to_phrases
from soramimic_video.project import Line, Note, Project, SongInfo
from soramimic_video.ruby import has_ruby, parse, segments, strip_ruby

RUBY_MARKS = ("｜", "|", "《", "》")


def _assert_no_markup(text: str) -> None:
    assert not any(m in text for m in RUBY_MARKS), f"ルビ記法が漏れている: {text!r}"


# ---- ruby モジュール ----


def test_parse_splits_plain_and_annotations():
    parsed = parse("｜今日《きょう》はいい天気")
    assert parsed.plain == "今日はいい天気"
    assert parsed.has_ruby
    assert [(s.start, s.end, s.reading) for s in parsed.spans] == [(0, 2, "キョウ")]


def test_parse_accepts_ascii_bar_and_hiragana_reading():
    # 開始記号は半角 | も受理し、読みはカタカナに正規化される
    assert parse("|明日《あした》").spans[0].reading == "アシタ"


def test_plain_text_is_returned_untouched():
    # 記法なしは「完全に従来どおり」: エスケープ解決すらせず入力そのもの
    for text in ("ふつうの歌詞", "《よみ》だけ", "｜だけ", r"バック\スラッシュ", ""):
        assert strip_ruby(text) == text
        assert not has_ruby(text)


def test_segments_concatenate_to_plain():
    parts = segments("春の｜季節《きせつ》に｜桜《さくら》が咲く")
    assert "".join(chunk for chunk, _ in parts) == "春の季節に桜が咲く"
    assert [forced for _, forced in parts] == [None, "キセツ", None, "サクラ", None]


def test_escaped_markup_is_literal_in_ruby_line():
    # 記法のある行ではエスケープが解決される(\｜ は文字の ｜)
    assert strip_ruby("｜今日《きょう》\\｜だけ") == "今日｜だけ"


# ---- reading.py(注釈の読みが効く) ----


def _reading_module():
    pytest.importorskip("MeCab")
    pytest.importorskip("unidic_lite")
    from soramimic_video import reading

    return reading


def test_text_to_kana_uses_annotation_reading():
    reading = _reading_module()
    # 「紅葉」は既定では コーヨー と読まれる。ルビで モミジ を強制できる
    assert reading.text_to_kana("紅葉") != "モミジ"
    assert reading.text_to_kana("｜紅葉《もみじ》") == "モミジ"
    assert reading.text_to_kana("秋の｜紅葉《もみじ》") == "アキノモミジ"


def test_text_to_kana_unidic_uses_annotation_reading():
    reading = _reading_module()
    assert reading.text_to_kana_unidic("｜紅葉《もみじ》が散る") == "モミジガチル"


def test_reading_candidates_agree_on_annotated_span():
    reading = _reading_module()
    # 両エンジンとも注釈の読みを使うので候補は増えない
    cands = reading.reading_candidates("｜紅葉《もみじ》")
    assert cands == ["モミジ"]


def test_reading_tokens_keeps_plain_surface():
    reading = _reading_module()
    pytest.importorskip("soramimic_yomi")
    tokens = reading.reading_tokens("秋の｜紅葉《もみじ》が散る")
    # 表層を連結すると素テキストに戻る(align の位置写像の前提)
    assert "".join(surf for surf, _ in tokens) == "秋の紅葉が散る"
    assert ("紅葉", "モミジ") in tokens


def test_reading_without_ruby_is_unchanged():
    reading = _reading_module()
    assert reading.text_to_kana("東京") == "トーキョー"
    assert reading.text_to_kana_unidic("広がって") == "ヒロガッテ"


# ---- align(表示は素テキスト・突き合わせは注釈の読み) ----


def _project_from_kana(lines_kana: list[str]) -> Project:
    notes: list[Note] = []
    lines: list[Line] = []
    nid = 0
    for li, kana in enumerate(lines_kana):
        ids = []
        for ch in kana:
            notes.append(
                Note(
                    id=nid, midi_note=60,
                    start_tick=nid * 480, end_tick=(nid + 1) * 480,
                    start_sec=nid * 0.5, end_sec=(nid + 1) * 0.5,
                    line=li, surface=ch, kana=ch, raw=ch,
                )
            )
            ids.append(nid)
            nid += 1
        lines.append(Line(id=li, xf_surface="", xf_kana=kana, note_ids=ids))
    return Project(
        song=SongInfo(midi_path="x.mid", ticks_per_beat=480), notes=notes, lines=lines
    )


def test_align_lines_stores_plain_text():
    _reading_module()
    project = _project_from_kana(["モミジガチル"])
    align_lines(project, ["｜紅葉《もみじ》が散る"])
    assert project.lines[0].original_text == "紅葉が散る"
    _assert_no_markup(project.lines[0].original_text or "")


def test_align_texts_matches_via_annotation_reading():
    _reading_module()
    # XF側は モミジ 読みのカナ。表記比較では当たらず、注釈の読みで初めて対応づく
    assert align_texts(["モミジガチル"], ["｜紅葉《もみじ》が散る"]) == [0]


def test_split_lyric_to_phrases_strips_markup():
    _reading_module()
    pieces = split_lyric_to_phrases(["アキノ", "モミジ"], "｜秋《あき》の｜紅葉《もみじ》")
    assert len(pieces) == 2
    assert "".join(pieces) == "秋の紅葉"
    for p in pieces:
        _assert_no_markup(p)


# ---- analyze-midi(ベース歌詞) → convert の実経路 ----


def _plain_midi(path: Path, notes: list[tuple[int, int, int]]) -> Path:
    mid = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    mid.tracks.append(track)
    events: list[tuple[int, Message]] = []
    for start, dur, note in notes:
        events.append((start, Message("note_on", channel=0, note=note, velocity=100)))
        events.append((start + dur, Message("note_off", channel=0, note=note, velocity=64)))
    events.sort(key=lambda e: e[0])
    prev = 0
    for tick, msg in events:
        track.append(msg.copy(time=tick - prev))
        prev = tick
    mid.save(str(path))
    return path


def test_build_from_melody_midi_ruby_forces_reading(tmp_path: Path):
    _reading_module()
    from soramimic_video.midi_project import build_from_melody_midi

    midi = _plain_midi(tmp_path / "m.mid", [(i * 240, 240, 60 + i) for i in range(3)])
    project = build_from_melody_midi(
        midi, tmp_path / "proj", lyrics="｜紅葉《もみじ》", render_backing=False
    )
    # 字幕・元歌詞は素テキスト、音符の読みは注釈どおり
    assert project.lines[0].original_text == "紅葉"
    assert project.lines[0].xf_surface == "紅葉"
    _assert_no_markup(project.lines[0].xf_surface)
    assert [n.kana for n in project.notes] == ["モ", "ミ", "ジ"]
    # 変換の入力になる xf_kana にも記法が残らない
    assert project.lines[0].xf_kana == "モミジ"


def test_convert_after_ruby_lyrics(tmp_path: Path):
    """ルビで直した読みが、そのまま実変換(convert_project)の入力になる。"""
    _reading_module()
    from soramimic_video.convert import convert_project
    from soramimic_video.midi_project import build_from_melody_midi

    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,もみじ饅頭,もみじ,モミジ\n1,鈴鹿,鈴鹿,スズカ",
        encoding="utf-8",
    )
    midi = _plain_midi(tmp_path / "m.mid", [(i * 240, 240, 60 + i) for i in range(3)])
    project = build_from_melody_midi(
        midi, tmp_path / "proj", lyrics="｜紅葉《もみじ》", render_backing=False
    )
    convert_project(project, wordlist=str(csv_path))
    assert project.parody is not None
    words = project.parody.lines[0].words
    assert words, "変換結果が空"
    # モミジ 読みなので「もみじ」が当たる(コーヨー読みなら当たらない)
    assert any(w.surface == "もみじ" for w in words)


def test_run_convert_accepts_ruby_notation(tmp_path: Path):
    """エンジン(soramimic)の記法対応そのもの: 記法つきフレーズを直接渡せる。"""
    from soramimic_video.soramimic_engine import run_convert

    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,もみじ饅頭,もみじ,モミジ\n1,鈴鹿,鈴鹿,スズカ",
        encoding="utf-8",
    )
    result = run_convert(["｜紅葉《もみじ》"], csv_path, None, {}, cache_db=False)
    units = result["lines"][0]["units"]
    # 注釈区間の読みが強制されている(既定の コーヨー ではない)
    assert "".join(u["pronunciation"] for u in units) == "モミジ"
    tokens = result["tokensList"][0]
    assert any(t.get("ruby") for t in tokens), "強制トークンに ruby フラグが立たない"


# ---- editor JSON のラウンドトリップ(ruby キーが落ちない) ----


def test_editor_roundtrip_keeps_ruby_key(tmp_path: Path):
    """editor(旧JS)を通ったJSONでも、video 側は tokensList を素通しする。"""
    import json

    from soramimic_video.editor_io import RAW_FILENAME, export_editor, save_raw
    from soramimic_video.project import Parody, ParodyLine, ParodyWord

    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,もみじ饅頭,もみじ,モミジ", encoding="utf-8"
    )
    project = _project_from_kana(["モミジ"])
    project.lines[0].xf_surface = "紅葉"
    project.parody = Parody(
        wordlist=str(csv_path),
        params={},
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="もみじ", kana="モミジ", original="もみじ饅頭",
                        original_surface="紅葉", originalkana="モミジ", note_ids=[0, 1, 2],
                    )
                ],
            )
        ],
    )
    raw = {
        "lines": [
            {
                "units": [
                    {"surface_form": "紅", "pronunciation": "モ", "phrase": True},
                    {"surface_form": "", "pronunciation": "ミ", "phrase": False},
                    {"surface_form": "葉", "pronunciation": "ジ", "phrase": False},
                ],
                "words": [
                    {
                        "surface": "もみじ", "kana": "モミジ", "original": "もみじ饅頭",
                        "original_surface": "紅葉", "originalkana": "モミジ",
                        "period": [0, 3],
                    }
                ],
            }
        ],
        "tokensList": [
            [
                {
                    "surface_form": "紅葉", "pronunciation": "モミジ",
                    "pos": "名詞", "ruby": True,
                }
            ]
        ],
        "phrases": ["モミジ"],
    }
    save_raw(raw, tmp_path)
    path = export_editor(project, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["tokensList"][0][0]["ruby"] is True

    # editor(旧JS)が書き戻したものを取り込んでも ruby キーが保存される
    from soramimic_video.editor_io import EDITOR_FILENAME, import_editor

    payload["wordlist"] = {"filepath": str(csv_path)}
    (tmp_path / EDITOR_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    import_editor(project, tmp_path)
    saved = json.loads((tmp_path / RAW_FILENAME).read_text(encoding="utf-8"))
    assert saved["tokensList"][0][0]["ruby"] is True


# ---- 字幕(ASS)に記法が漏れない ----


def test_editor_preview_subtitles_are_plain(tmp_path: Path):
    _reading_module()
    from soramimic_video.editor_io import build_editor_preview
    from soramimic_video.layout import load_layout

    payload = {
        "phrases": ["モミジガチル"],
        "results": [
            [
                {
                    "surface": "もみじ", "kana": "モミジ", "original": "もみじ饅頭",
                    "original_surface": "紅葉", "originalkana": "モミジ",
                }
            ]
        ],
        "unitsList": [[]],
        "wordlist": {"filepath": "wordlists/none.csv"},
    }
    preview = build_editor_preview(
        payload, None, load_layout(None), lyrics="｜紅葉《もみじ》が散る"
    )
    assert preview["cues"], "プレビューのキューが空"
    for cue in preview["cues"]:
        _assert_no_markup(cue["original_text"])
    assert "紅葉が散る" in preview["cues"][0]["original_text"]
