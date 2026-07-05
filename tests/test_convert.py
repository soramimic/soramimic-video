from pathlib import Path

import pytest

from soramimi_video.convert import (
    BRIDGE_DIR,
    _map_word_to_notes,
    _offset_map,
    convert_project,
)
from soramimi_video.kana import split_moras
from soramimi_video.project import Line, Note, Project, SongInfo


def test_split_moras():
    assert split_moras("グミョウジ") == ["グ", "ミョ", "ウ", "ジ"]
    assert split_moras("トーキョー") == ["トー", "キョー"]
    assert split_moras("シズ") == ["シ", "ズ"]


def test_offset_map_identity():
    assert _offset_map("アイウ", "アイウ") == [0, 1, 2, 3]


def test_offset_map_with_gap():
    # src側に1文字余分がある(dstに無い)ケースでも単調な対応になる
    table = _offset_map("アイXウ", "アイウ")
    assert table[0] == 0 and table[2] == 2 and table[4] == 3
    assert all(table[i] <= table[i + 1] for i in range(len(table) - 1))


def test_map_word_to_notes():
    # ユニット: [シ, ズ, ム, ヨウ, ニ] / 音符kana: [シ, ズ, ム, ヨ, ウ, ニ]
    unit_lens = [1, 1, 1, 2, 1]
    note_lens = [1, 1, 1, 1, 1, 1]
    identity = list(range(7))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 2), ["シ", "ズ"], "シズ"
    )
    assert ids == [0, 1]
    assert kana == ["シ", "ズ"]
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (2, 5), ["グ", "ミョ", "ウ", "ジ"], "ムヨウニ"
    )
    assert ids == [2, 3, 4, 5]
    assert kana == ["グ", "ミョ", "ウ", "ジ"]


def test_map_word_to_notes_syllable_source():
    # 「フェンス」(4文字・3音節)に3要素の発音が対応するケース
    unit_lens = [2, 1, 1]  # フェ, ン, ス
    note_lens = [2, 1, 1]
    identity = list(range(5))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 3), ["ホ", "ン", "ス"], "フェンス"
    )
    assert ids == [0, 1, 2]
    assert kana == ["ホ", "ン", "ス"]


def test_map_word_to_notes_multi_kana_note():
    # 音符kanaが2文字(「ライ」が1音符)のケース: 2要素が同じ音符にまとまる
    unit_lens = [1, 1, 1]
    note_lens = [1, 2]  # [キ, ライ]
    identity = list(range(4))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 3), ["ミ", "ラ", "イ"], "キラウ"
    )
    assert ids == [0, 1]
    assert kana == ["ミ", "ライ"]


def _tiny_project() -> Project:
    notes = []
    kanas = ["シ", "ズ", "ム"]
    for i, k in enumerate(kanas):
        notes.append(
            Note(
                id=i, midi_note=60 + i,
                start_tick=i * 480, end_tick=(i + 1) * 480,
                start_sec=i * 0.5, end_sec=(i + 1) * 0.5,
                line=0, surface=k, kana=k, raw=k,
            )
        )
    lines = [Line(id=0, xf_surface="シズム", xf_kana="シズム", note_ids=[0, 1, 2])]
    return Project(
        song=SongInfo(midi_path="x.mid", ticks_per_beat=480), notes=notes, lines=lines
    )


@pytest.mark.skipif(
    not (BRIDGE_DIR / "node_modules").exists(),
    reason="bridge未セットアップ(cd bridge && npm ci)",
)
def test_convert_project_with_bridge(tmp_path: Path):
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,静岡駅,静岡,シズオカ\n1,鈴鹿,鈴鹿,スズカ",
        encoding="utf-8",
    )
    project = _tiny_project()
    convert_project(project, wordlist=str(csv_path))
    assert project.parody is not None
    words = project.parody.lines[0].words
    assert words, "変換結果が空"
    for w in words:
        assert w.note_ids, "音符への対応づけがない"
        assert set(w.note_ids) <= {0, 1, 2}
