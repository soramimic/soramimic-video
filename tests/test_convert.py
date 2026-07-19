import json
from collections import Counter
from pathlib import Path

from soramimic_video.convert import (
    _align_positions,
    _coerce_params,
    _dropout_flags,
    _map_word_to_notes,
    _offset_map,
    _pair_score,
    _paired_sokuon,
    apply_converted_lines,
    convert_project,
    parse_convert_params,
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


def test_parse_convert_params():
    # 基本: KEY=VALUE を dict に
    assert parse_convert_params("DUPLICATE=true") == {"DUPLICATE": "true"}
    # 複数区切り(改行・セミコロン・縦棒)、前後空白の除去
    assert parse_convert_params("DUPLICATE=false\nLENGTH = 2 ; REPEAT=50") == {
        "DUPLICATE": "false",
        "LENGTH": "2",
        "REPEAT": "50",
    }
    assert parse_convert_params("A=1|B=2") == {"A": "1", "B": "2"}
    # '=' を含まない要素・空キー・空文字は無視
    assert parse_convert_params("") == {}
    assert parse_convert_params(None) == {}
    assert parse_convert_params("garbage;;=x; K=v") == {"K": "v"}
    # 値に '=' が含まれても最初の '=' で分割する
    assert parse_convert_params("WHERE=a=b") == {"WHERE": "a=b"}


def test_parse_convert_params_ui_payload_all_defaults():
    # スコア項目がすべて「既定」のとき UI は DUPLICATE だけを送る。
    # 各スコアパラメータのキーは含まれない=エンジン既定のまま(現行出力と一致)。
    coerced = _coerce_params(parse_convert_params("DUPLICATE=false"))
    assert coerced == {"DUPLICATE": False}
    for key in (
        "SAME_VOWEL_REWARD",
        "SAME_CONSONANT_REWARD",
        "SAME_PHRASE_BREAK_REWARD",
        "WORD_NUMBER_PENALTY",
    ):
        assert key not in coerced


def test_parse_convert_params_ui_payload_selected():
    # 各スコア項目を選んだときに送られる値(本家app.js由来の対応表のスポットチェック)。
    #   母音 大 → SAME_VOWEL_REWARD=0.1、子音 小 → SAME_CONSONANT_REWARD=1、
    #   文節 中 → SAME_PHRASE_BREAK_REWARD=30、単語長 大 → WORD_NUMBER_PENALTY=60
    spec = (
        "DUPLICATE=true\nSAME_VOWEL_REWARD=0.1\nSAME_CONSONANT_REWARD=1\n"
        "SAME_PHRASE_BREAK_REWARD=30\nWORD_NUMBER_PENALTY=60"
    )
    coerced = _coerce_params(parse_convert_params(spec))
    assert coerced["DUPLICATE"] is True
    assert coerced["SAME_VOWEL_REWARD"] == 0.1
    assert coerced["SAME_CONSONANT_REWARD"] == 1  # (10-0)*0.1 の "1"
    assert coerced["SAME_PHRASE_BREAK_REWARD"] == 30
    assert coerced["WORD_NUMBER_PENALTY"] == 60


def test_coerce_params_types():
    out = _coerce_params(
        {"DUPLICATE": "true", "OFF": "False", "N": "3", "F": "0.5", "S": "hello"}
    )
    assert out["DUPLICATE"] is True
    assert out["OFF"] is False
    assert out["N"] == 3 and isinstance(out["N"], int)
    assert out["F"] == 0.5 and isinstance(out["F"], float)
    assert out["S"] == "hello"


def test_convert_project_default_duplicate_preserved(tmp_path: Path):
    # パラメータ未指定なら現行どおり DUPLICATE=False が保たれる(後方互換)
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,静岡駅,静岡,シズオカ\n1,鈴鹿,鈴鹿,スズカ",
        encoding="utf-8",
    )
    project = _tiny_project()
    convert_project(project, wordlist=str(csv_path))
    assert project.parody is not None
    assert project.parody.params["DUPLICATE"] is False


def test_convert_project_passes_params_through(tmp_path: Path):
    # 文字列パラメータが型変換されて変換エンジン・parody に渡る
    csv_path = tmp_path / "words.csv"
    csv_path.write_text(
        "id,original,surface,pronunciation\n0,静岡駅,静岡,シズオカ\n1,鈴鹿,鈴鹿,スズカ",
        encoding="utf-8",
    )
    project = _tiny_project()
    convert_project(
        project, wordlist=str(csv_path), params={"DUPLICATE": "true", "LENGTH": "2"}
    )
    assert project.parody is not None
    assert project.parody.params["DUPLICATE"] is True  # 明示指定で既定を上書き
    assert project.parody.params["LENGTH"] == 2  # int へ型変換


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


# --- 母音一致優先の単語内アライメント(DP) ---


def test_dropout_flags():
    # 特殊モーラ(ッ/ン/ー)は常に脱落しやすい
    assert _dropout_flags(["ン", "ッ", "ー"]) == [True, True, True]
    # エイ型(e段+イ)・オウ型(o段+ウ)の2モーラ目
    assert _dropout_flags(["セ", "イ"]) == [False, True]
    assert _dropout_flags(["コ", "ウ"]) == [False, True]
    # ア段+イ・イ段+ウ 等は連鎖でないので脱落扱いしない
    assert _dropout_flags(["カ", "イ"]) == [False, False]
    assert _dropout_flags(["シ", "ア"]) == [False, False]


def test_dropout_flags_bare_vowel_only():
    # 連鎖2モーラ目とみなすのは母音単独のかな(イ/ウ)だけ。子音付きの i段/u段
    # モーラ(ディ・ク等)は直前がe段/o段でも独立モーラなので対象外にする。
    assert _dropout_flags(["レ", "ディ"]) == [False, False]  # レ+ディ 対象外
    assert _dropout_flags(["レ", "イ"]) == [False, True]  # レ+イ  対象
    assert _dropout_flags(["コ", "ウ"]) == [False, True]  # コ+ウ  対象
    assert _dropout_flags(["コ", "ク"]) == [False, False]  # コ+ク  対象外
    # 他の子音付き i段/u段 も対象外
    assert _dropout_flags(["キ", "リ"]) == [False, False]
    assert _dropout_flags(["ホ", "ル"]) == [False, False]


def test_map_word_to_notes_consonant_i_mora_not_absorbed():
    # 「レディ」相当: 元ノート[レ, テ, イ] に要素[レ, ディ]。ディは子音付きi段モーラ
    # なので脱落調整の対象外。母音一致(ディ=イ → イ音符)で イ音符に載り、安易に
    # ー化されない(テ音符が継続ーになる)。
    ids, kana = _map_word_to_notes(
        [1, 2], [1, 1, 1], list(range(4)), (0, 2),
        ["レ", "ディ"], "レディ", notes_kana=["レ", "テ", "イ"],
    )
    assert ids == [0, 1, 2]
    assert kana == ["レ", "", "ディ"]  # ディはイ音符へ、テ音符がー
    assert "ディ" in kana  # ディが実音として残る(ー化されていない)


def test_pair_score_vowel_dominates_consonant():
    # 母音一致(重み1000)は子音一致(10)+脱落調整(<=2)より必ず大きい
    v_match = _pair_score("ウ", "ウ", False, False)  # 母音一致
    c_only = _pair_score("ウ", "ト", False, False)  # 母音不一致(トはオ段)
    assert v_match >= 1000 > c_only
    # 子音一致は脱落調整より大きい(第2キー)
    assert _pair_score("サ", "ソ", False, False) > _pair_score("サ", "ト", False, False)


def test_align_positions_picks_matching_note():
    # 単一要素・2音符: スコアの高い音符を選ぶ
    assert _align_positions([[1000, 10]], 1, 2, force_first=False) == [0]
    assert _align_positions([[10, 1000]], 1, 2, force_first=False) == [1]
    # force_first のときは先頭音符に固定(語頭の継続ー化を避ける)
    assert _align_positions([[10, 1000]], 1, 2, force_first=True) == [0]
    # 同点は前方優先
    assert _align_positions([[500, 500]], 1, 2, force_first=False) == [0]
    # 2要素・3音符: 単調増加で総和最大
    assert _align_positions([[1000, 0, 0], [0, 0, 1000]], 2, 3, force_first=False) == [0, 2]


def test_map_word_to_notes_vowel_alignment():
    # 非先頭ユニットで、1要素「ウ」を2音符[ト(オ), ウ(ウ)]へ載せる。
    # 母音一致でウ音符を選び、あいだのト音符は継続ーになる。
    ids, kana = _map_word_to_notes(
        [1, 2], [1, 1, 1], list(range(4)), (0, 2),
        ["ソ", "ウ"], "ソウ", notes_kana=["ソ", "ト", "ウ"],
    )
    assert ids == [0, 1, 2]
    assert kana == ["ソ", "", "ウ"]
    # notes_kana を渡さないと従来の左詰め(位置ベース)のまま
    ids2, kana2 = _map_word_to_notes(
        [1, 2], [1, 1, 1], list(range(4)), (0, 2), ["ソ", "ウ"], "ソウ",
    )
    assert ids2 == [0, 1, 2]
    assert kana2 == ["ソ", "ウ", ""]


def test_map_word_to_notes_special_mora_alignment():
    # 促音「ッ」を、元音符の促音位置へ寄せる(特殊モーラの母音一致 sp==sp)。
    # 要素[バ,ッ,ト] を音符[バ,キ,ッ,ト] へ。DPは ッ→ッ、ト→ト を選び、キは継続ー。
    ids, kana = _map_word_to_notes(
        [1, 3], [1, 1, 1, 1], list(range(5)), (0, 2),
        ["バ", "ッ", "ト"], "バット", notes_kana=["バ", "キ", "ッ", "ト"],
    )
    assert ids == [0, 1, 2, 3]
    assert kana == ["バ", "", "ッ", "ト"]
    # 従来(位置ベース)は ッ→キ、ト→ッ とずれていた
    _, kana_old = _map_word_to_notes(
        [1, 3], [1, 1, 1, 1], list(range(5)), (0, 2), ["バ", "ッ", "ト"], "バット",
    )
    assert kana_old == ["バ", "ッ", "ト", ""]


def test_map_word_to_notes_dp_keeps_ids_and_count():
    # DPの有無で音符集合(ids)とモーラ数は不変。変わるのは載せ先だけ。
    args = ([1, 2], [1, 1, 1], list(range(4)), (0, 2), ["ソ", "ウ"], "ソウ")
    ids_dp, kana_dp = _map_word_to_notes(*args, notes_kana=["ソ", "ト", "ウ"])
    ids_pos, kana_pos = _map_word_to_notes(*args)
    assert ids_dp == ids_pos
    # 実モーラ数(継続ー以外)も不変
    assert sum(k != "" for k in kana_dp) == sum(k != "" for k in kana_pos)


def test_dp_and_shared_note_resolution_coexist(tmp_path: Path):
    # DPが動く複数音符ユニットと、複合音符の二重割り当てが同居しても、
    # 解消(_resolve_shared_notes)が正しく機能し、音符が重複しないこと。
    # 音符 [ソ, ト, ウナ(複合), コ]、単語「ソウ」(period0..2) と「ナコ」(period2..4)。
    #   「ソウ」: ソ→ソ, ウ は ト(オ)/ウ(ウ) から母音一致でウ側を取る。
    #   複合音符「ウナ」は境界を跨ぐが、_resolve_shared_notes が一方へ一本化する。
    project = _line_project(["ソ", "ト", "ウナ", "コ"])  # note_concat="ソトウナコ"
    converted = [
        {
            "units": [{"pronunciation": c} for c in "ソトウナコ"],
            "words": [
                {"surface": "ソウ", "kana": "ソウ", "period": [0, 3],
                 "pronunciation": ["ソ", "ウ"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
                {"surface": "ナコ", "kana": "ナコ", "period": [3, 5],
                 "pronunciation": ["ナ", "コ"], "original": "", "original_surface": "",
                 "originalkana": "", "locked": False},
            ],
        }
    ]
    apply_converted_lines(
        project, converted, wordlist=_empty_wordlist(tmp_path), where=None, params={}
    )
    _assert_no_shared_notes(project)


# --- 2段DP(音節への個数配分)+ 促音ッ閉音節ハード制約 ---


def _fill_bar(kana: list[str]) -> list[str]:
    return [k or "ー" for k in kana]


def test_syllable_distribution_hosoura():
    # ホソウラ: 音節[ホン,トウ,ハ](音符[ホ,ン,ト,ウ,ハ])に要素[ホ,ソ,ウ,ラ]。
    # 位置ベースだと ホン=[ホ,ソ] でソがン音符に載る。外側DPは配分[1,2,1]を選び、
    # トウ音節に[ソ,ウ]を載せて ソ→ト(オ一致)、ウ→ウ、ホン音節はホのみ(ンはー)。
    ids, kana = _map_word_to_notes(
        [2, 2, 1], [1, 1, 1, 1, 1], list(range(6)), (0, 3),
        ["ホ", "ソ", "ウ", "ラ"], "ホソウラ",
        notes_kana=["ホ", "ン", "ト", "ウ", "ハ"],
    )
    assert ids == [0, 1, 2, 3, 4]
    assert _fill_bar(kana) == ["ホ", "ー", "ソ", "ウ", "ラ"]


def test_syllable_distribution_no_word_initial_bar():
    # 語頭音節が空(語頭ー)にならない(語頭固定)。
    ids, kana = _map_word_to_notes(
        [1, 1, 1, 1, 2, 1], [1] * 7, list(range(8)), (0, 6),
        ["ク", "ル", "ヤ", "バ", "ッ", "ト"], "クルヤバット",
        notes_kana=["ボ", "ク", "ラ", "ハ", "キ", "ッ", "ト"],
    )
    assert kana[0] != ""  # 先頭音符は継続ーにしない


def test_paired_sokuon_flags():
    # 促音ッが直前モーラと閉音節を成す位置のみ True
    assert _paired_sokuon(["ク", "ル", "ヤ", "バ", "ッ", "ト"]) == [
        False, False, False, False, True, False
    ]
    # 語末ッは対象外(安全側フォールバック)
    assert _paired_sokuon(["バ", "ッ"]) == [False, False]
    # ッッ連続の2つ目(直前がッ)は対象外
    assert _paired_sokuon(["バ", "ッ", "ッ", "ト"]) == [False, True, False, False]
    # 語頭ッは対象外
    assert _paired_sokuon(["ッ", "ト", "ア"]) == [False, False, False]


def test_sokuon_closed_syllable_pulls_x_into_syllable():
    # クルヤバット: 促音ッは直前バと不可分。バがハ音符に載るとキ音符を挟むため不可。
    # 制約により バッ はキッ音節(音符[キ,ッ])へ引き込まれ [ク,ル,ヤ,ー,バ,ッ,ト]。
    # (母音一致はバ/ハ一致を失い1点減るが、閉音節ハード制約が優先される。)
    ids, kana = _map_word_to_notes(
        [1, 1, 1, 1, 2, 1], [1] * 7, list(range(8)), (0, 6),
        ["ク", "ル", "ヤ", "バ", "ッ", "ト"], "クルヤバット",
        notes_kana=["ボ", "ク", "ラ", "ハ", "キ", "ッ", "ト"],
    )
    assert _fill_bar(kana) == ["ク", "ル", "ヤ", "ー", "バ", "ッ", "ト"]
    # バとッが隣接音符(間にーを挟まない)
    bi = kana.index("バ")
    assert kana[bi + 1] == "ッ"


def test_sokuon_pair_stays_adjacent_within_syllable():
    # キッ音節[キ,ッ]に[バ,ッ]、ト音節に[ト]。促音ッが元のッ音符に載る。
    ids, kana = _map_word_to_notes(
        [2, 1], [1, 1, 1], list(range(4)), (0, 2),
        ["バ", "ッ", "ト"], "バット", notes_kana=["キ", "ッ", "ト"],
    )
    assert _fill_bar(kana) == ["バ", "ッ", "ト"]


def test_leadi_consonant_i_still_ok_after_distribution():
    # レディ(子音付きi段は脱落対象外)が2段DP+制約下でも [レ,ー,ディ] のまま。
    ids, kana = _map_word_to_notes(
        [1, 2], [1, 1, 1], list(range(4)), (0, 2),
        ["レ", "ディ"], "レディ", notes_kana=["レ", "テ", "イ"],
    )
    assert _fill_bar(kana) == ["レ", "ー", "ディ"]


def test_overflow_sokuon_pairs_with_closed_note():
    # ラグナット(夜に駆ける実例): 音節[ダ,ケ,ダッ,タ]に要素[ラ,グ,ナ,ッ,ト](溢れ1)。
    # 従来は末尾寄せで ラ|グ|ナ|ット(ッが音符頭)だった。分割DPは閉音節ダッに
    # ナッを載せ、ト は タ に1モーラで対応する。
    ids, kana = _map_word_to_notes(
        [1, 1, 2, 1], [1, 1, 2, 1], list(range(6)), (0, 4),
        ["ラ", "グ", "ナ", "ッ", "ト"], "ラグナット",
        notes_kana=["ダ", "ケ", "ダッ", "タ"],
    )
    assert ids == [0, 1, 2, 3]
    assert kana == ["ラ", "グ", "ナッ", "ト"]


def test_overflow_hatsuon_pairs_with_closed_note():
    # アンカー(実例): 音節[アッ,タ]に要素[ア,ン,カー](溢れ1)。
    # ア|ンカー ではなく アン|カー(閉音節アッに撥音ンを積む)。
    ids, kana = _map_word_to_notes(
        [2, 1], [2, 1], list(range(4)), (0, 2),
        ["ア", "ン", "カー"], "アンカー",
        notes_kana=["アッ", "タ"],
    )
    assert ids == [0, 1]
    assert kana == ["アン", "カー"]


def test_overflow_double_stack_mirrors_note_moras():
    # ランエイサン(実例): 音節[ナッ,テイ,タ]に要素[ラ,ン,エ,イ,サー](溢れ2)。
    # 従来は ラ|ン|エイサー。分割DPは ナッ↔ラン、テイ↔エイ を対応させる。
    ids, kana = _map_word_to_notes(
        [2, 2, 1], [2, 2, 1], list(range(6)), (0, 3),
        ["ラ", "ン", "エ", "イ", "サー"], "ランエイサン",
        notes_kana=["ナッ", "テイ", "タ"],
    )
    assert ids == [0, 1, 2]
    assert kana == ["ラン", "エイ", "サー"]


def test_overflow_without_notes_kana_keeps_legacy():
    # notes_kana が無いときは従来どおり末尾寄せのまま
    ids, kana = _map_word_to_notes(
        [1, 1, 2, 1], [1, 1, 2, 1], list(range(6)), (0, 4),
        ["ラ", "グ", "ナ", "ッ", "ト"], "ラグナット",
    )
    assert ids == [0, 1, 2, 3]
    assert kana == ["ラ", "グ", "ナ", "ット"]


def test_dropout_flags_and_pair_score_tolerate_empty_kana():
    # ルビ無し漢字ノートでkanaが空になるXF(例: 女々しくて)でも落ちない
    assert _dropout_flags(["", "イ", "ウ"]) == [False, False, False]
    from soramimic_video.convert import _pair_score
    assert _pair_score("", "カ", False, False) == 0
    assert _pair_score("カ", "", False, False) == 0
