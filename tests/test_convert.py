from pathlib import Path

from soramimic_video.convert import (
    _map_word_to_notes,
    _offset_map,
    convert_project,
)
from soramimic_video.kana import split_moras
from soramimic_video.project import Line, Note, Project, SongInfo


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
        unit_lens, note_lens, identity, (2, 5), ["グ", "ミョ", "ウ", "ジ"], "グミョウジ"
    )
    assert ids == [2, 3, 4, 5]
    assert kana == ["グ", "ミョ", "ウ", "ジ"]


def test_map_word_to_notes_syllable_source():
    # 「フェンス」(4文字・3音節)に3要素の発音が対応するケース
    unit_lens = [2, 1, 1]  # フェ, ン, ス
    note_lens = [2, 1, 1]
    identity = list(range(5))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 3), ["ホ", "ン", "ス"], "ホンス"
    )
    assert ids == [0, 1, 2]
    assert kana == ["ホ", "ン", "ス"]


def test_map_word_to_notes_multi_kana_note():
    # 音符kanaが2文字(「ライ」が1音符)のケース: 2要素が同じ音符にまとまる
    unit_lens = [1, 1, 1]
    note_lens = [1, 2]  # [キ, ライ]
    identity = list(range(4))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 3), ["ミ", "ラ", "イ"], "ミライ"
    )
    assert ids == [0, 1]
    assert kana == ["ミ", "ライ"]


def _fill(kana: list[str]) -> list[str]:
    """apply_converted_lines 後段の継続音符ー埋めを再現する。"""
    return [k or "ー" for k in kana]


def test_map_word_to_notes_restore_compressed_moras():
    # ふるさと3行目「夢は今もめぐりて」相当。元歌詞ユニット [マ][モ][メエ][グウ][リイ][テ]
    # (9音符)に、単語「アンドレイリンデ」pron=[アー,ド,レ,イ,リー,デ] を載せる。
    # リン(要素リー)は空き音符が1つあるので「リ」「ン」に復元される。
    # アン(要素アー)は復元先(マ=1音符)が無いので現状どおり「アー」のまま。
    unit_lens = [1, 1, 2, 2, 2, 1]  # マ モ メエ グウ リイ テ
    note_lens = [1] * 9
    identity = list(range(10))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 6),
        ["アー", "ド", "レ", "イ", "リー", "デ"], "アンドレイリンデ",
    )
    assert ids == list(range(9))
    assert kana == ["アー", "ド", "レ", "", "イ", "", "リ", "ン", "デ"]
    assert _fill(kana) == ["アー", "ド", "レ", "ー", "イ", "ー", "リ", "ン", "デ"]


def test_map_word_to_notes_unit_boundary_alignment():
    # ハビー(ズレの実例): 元歌詞ユニット [ハ][イイ](3音符1:1)に pron=[ハ,ビー]。
    # 要素はユニット単位で載るので ビー は2番目のユニット先頭(2音符目)に来る。
    # 旧実装は fine モーラ吸収でビーが3音符目にずれて「ハー・ビー」になっていた。
    unit_lens = [1, 2]  # ハ イイ
    note_lens = [1, 1, 1]
    identity = list(range(4))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 2), ["ハ", "ビー"], "ハビー"
    )
    assert ids == [0, 1, 2]
    assert kana == ["ハ", "ビー", ""]
    assert _fill(kana) == ["ハ", "ビー", "ー"]


def test_map_word_to_notes_multi_element_unit():
    # 劉(kana リュウ): 1ユニット [ユウ](2音符)に pron=[リュ,ウ] の2要素。
    # 要素数>ユニット数なので、単一ユニット内の2音符へ 1:1 に分かれる。
    unit_lens = [2]  # ユウ
    note_lens = [1, 1]
    identity = list(range(3))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 1), ["リュ", "ウ"], "リュウ"
    )
    assert ids == [0, 1]
    assert kana == ["リュ", "ウ"]


def test_map_word_to_notes_restore_final_n():
    # 語末ンの復元: ハイン(kana ハイン)、pron=[ハ,イー]、元歌詞 [ハ][イイ]。
    # 要素イーは単語側で イ+ン を圧縮した形なので、イイユニットの空き音符に
    # ンを復元して「ハ・イ・ン」になる。
    unit_lens = [1, 2]  # ハ イイ
    note_lens = [1, 1, 1]
    identity = list(range(4))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 2), ["ハ", "イー"], "ハイン"
    )
    assert ids == [0, 1, 2]
    assert kana == ["ハ", "イ", "ン"]


def test_map_word_to_notes_no_empty_no_restore():
    # 空き音符が無い場合は圧縮された撥音を復元できない(現状どおり長音)。
    # アン(要素アー)を1音符のユニット [マ] に載せる。
    unit_lens = [1]  # マ
    note_lens = [1]
    identity = list(range(2))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 1), ["アー"], "アン"
    )
    assert ids == [0]
    assert kana == ["アー"]


def test_map_word_to_notes_no_compression_keeps_bar():
    # 圧縮が起きていない(単語が枠より短い)場合、空き音符は復元されず継続(ー)。
    # 単語「モ」を長いユニット [モオ](2音符)に載せる。
    unit_lens = [2]  # モオ
    note_lens = [1, 1]
    identity = list(range(3))
    ids, kana = _map_word_to_notes(
        unit_lens, note_lens, identity, (0, 1), ["モ"], "モ"
    )
    assert ids == [0, 1]
    assert kana == ["モ", ""]
    assert _fill(kana) == ["モ", "ー"]


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


def test_convert_project(tmp_path: Path):
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
