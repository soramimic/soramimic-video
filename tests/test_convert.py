import json
from collections import Counter
from pathlib import Path

from soramimic_video.convert import (
    _map_word_to_notes,
    _offset_map,
    apply_converted_lines,
    convert_project,
)
from soramimic_video.kana import split_moras
from soramimic_video.project import Line, Note, Project, SongInfo

FIXTURES = Path(__file__).parent / "fixtures"


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


def _empty_wordlist(tmp_path: Path) -> str:
    csv_path = tmp_path / "words.csv"
    csv_path.write_text("id,original,surface,pronunciation\n", encoding="utf-8")
    return str(csv_path)


def _line_project(kanas: list[str]) -> Project:
    """音符kana列だけを持つ1行プロジェクト(id==indexの音符)。"""
    notes = [
        Note(
            id=i, midi_note=60, start_tick=i * 480, end_tick=(i + 1) * 480,
            start_sec=i * 0.5, end_sec=(i + 1) * 0.5,
            line=0, surface=k, kana=k, raw=k,
        )
        for i, k in enumerate(kanas)
    ]
    xf = "".join(kanas)
    lines = [Line(id=0, xf_surface=xf, xf_kana=xf, note_ids=list(range(len(kanas))))]
    return Project(
        song=SongInfo(midi_path="x.mid", ticks_per_beat=480), notes=notes, lines=lines
    )


def _assert_no_shared_notes(project: Project) -> None:
    """各行で「行の音符数 == 単語のnote_ids合計(重複なし)」と、各note_idが
    高々1単語にしか属さないことを検証する。"""
    assert project.parody is not None
    for pline in project.parody.lines:
        counts: Counter[int] = Counter()
        for w in pline.words:
            assert len(w.note_ids) == len(w.note_kana), "note_ids と note_kana が不揃い"
            counts.update(w.note_ids)
        for nid, c in counts.items():
            assert c == 1, f"音符 {nid} が {c} 単語に二重割り当て"


def test_apply_converted_lines_resolves_compound_note_double_assignment(
    tmp_path: Path,
):
    # 複合音符(kana2文字)が単語境界を跨ぐケース。
    # 音符 [ア, ロガ, ト] (中央の「ロガ」が2文字の複合音符)に、
    # 単語「アロ」(period 0..2, ア/ロ) と 単語「ガト」(period 2..4, ガ/ト) を載せる。
    # 中央音符 index1 は両単語にヒットするが、
    #   「アロ」の文字被り: [0,3) ∩ [1,3) = 2
    #   「ガト」の文字被り: [3,5) ∩ [1,3) = 0
    # なので「アロ」が音符1を取り、「ガト」からは外れる。
    project = _line_project(["ア", "ロガ", "ト"])  # note_concat = "アロガト"
    converted = [
        {
            "units": [{"pronunciation": c} for c in "アロガト"],
            "words": [
                {"surface": "アロ", "kana": "アロ", "period": [0, 2],
                 "pronunciation": ["ア", "ロ"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
                {"surface": "ガト", "kana": "ガト", "period": [2, 4],
                 "pronunciation": ["ガ", "ト"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
            ],
        }
    ]
    apply_converted_lines(
        project, converted, wordlist=_empty_wordlist(tmp_path), where=None, params={}
    )
    _assert_no_shared_notes(project)
    words = {w.surface: w for w in project.parody.lines[0].words}
    # 「アロ」が複合音符(index1)を保持し、末尾モーラ「ロ」を歌う
    assert words["アロ"].note_ids == [0, 1]
    assert words["アロ"].note_kana == ["ア", "ロ"]
    # 「ガト」は複合音符を失い、後続音符だけを持つ(モーラ数が1減る)
    assert words["ガト"].note_ids == [2]
    assert words["ガト"].note_kana == ["ト"]


def test_apply_converted_lines_tiebreak_favors_earlier_word(tmp_path: Path):
    # 文字被りが同点(各1)なら先行単語が複合音符を取る。
    # 音符 [ア, ロナ, ト]、単語「アロ」(period0..2) と「ナト」(period2..4)。
    #   「アロ」被り: [0,2) ∩ [1,3) = 1、  「ナト」被り: [2,5) ∩ [1,3) = 1 → 同点
    project = _line_project(["ア", "ロナ", "ト"])
    converted = [
        {
            "units": [{"pronunciation": c} for c in "アロナト"],
            "words": [
                {"surface": "アロ", "kana": "アロ", "period": [0, 2],
                 "pronunciation": ["ア", "ロ"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
                {"surface": "ナト", "kana": "ナト", "period": [2, 4],
                 "pronunciation": ["ナ", "ト"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
            ],
        }
    ]
    apply_converted_lines(
        project, converted, wordlist=_empty_wordlist(tmp_path), where=None, params={}
    )
    _assert_no_shared_notes(project)
    words = {w.surface: w for w in project.parody.lines[0].words}
    assert words["アロ"].note_ids == [0, 1]  # 先行単語が複合音符を取る
    assert words["ナト"].note_ids == [2]


def test_apply_converted_lines_be429b67_fixture(tmp_path: Path):
    # be429b67(実データ)から抜粋した2行。複合音符「ナイ」が単語境界を跨ぎ、
    # 修正前は隣接2単語へ二重割り当てされて先行単語の末尾モーラが潰れていた。
    fixture = json.loads(
        (FIXTURES / "be429b67_shared_notes.json").read_text(encoding="utf-8")
    )
    wordlist = _empty_wordlist(tmp_path)
    expected_first = {18: ("オロガ", "ガ"), 26: ("ウルナ", "ナ")}
    for entry in fixture:
        project = _line_project(entry["notes_kana"])
        apply_converted_lines(
            project, [entry["converted"]], wordlist=wordlist, where=None, params={}
        )
        _assert_no_shared_notes(project)
        ci = entry["compound_note_local_index"]
        surface, mora = expected_first[entry["line_id"]]
        # 複合音符は先行単語が保持し、実モーラを歌う(「ー」潰れでない)
        holder = [
            w for w in project.parody.lines[0].words if ci in w.note_ids
        ]
        assert len(holder) == 1, "複合音符が単一単語に属すること"
        w = holder[0]
        assert w.surface == surface
        assert w.note_kana[w.note_ids.index(ci)] == mora
