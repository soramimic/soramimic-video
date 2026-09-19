from soramimic_video.kana_whisper import (
    build_kana_contexts,
    choose_reading,
    normalize_kana_evidence,
)


def test_normalize_kana_evidence_removes_non_kana_and_pronunciation_spelling():
    assert normalize_kana_evidence("何? なにを、みていたの") == "ナニオミテータノ"


def test_mix_tie_and_vocals_support_choose_nani_reading():
    decision = choose_reading(
        ["ナンヲシテイタノ", "ナニヲシテイタノ"],
        [
            "ウタビアフレテヤマナイノワナミダナカナニオシテイタノナニオ",
            "ウタビアフレテヤマナイノワナミラダケナニモシテイタノナニオ",
        ],
    )
    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"


def test_exact_soba_evidence_beats_gawa_default():
    decision = choose_reading(
        ["アンナニガワニイタノニ", "アンナニソバニイタノニ"],
        ["アンナニソバニイタノニ", "アンナニソバニイタノニ"],
    )
    assert decision.selected_index == 1


def test_conflicting_views_keep_default():
    decision = choose_reading(
        ["ココデ", "ソコデ"],
        ["ソコデ", "ココデ"],
    )
    assert decision.selected_index == 0
    assert decision.reason == "default-or-tie"


def test_different_mora_count_can_be_selected_from_closed_candidates():
    decision = choose_reading(["ヨルヨル", "ヨ"], ["ヨ", "ヨ"])
    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"


def test_weak_voicing_evidence_keeps_default():
    decision = choose_reading(
        ["キレーゴトジャナイケド", "キレーコトジャナイケド"],
        ["チレーコトジャナイケド", "チレーコトジャナイケド"],
    )
    assert decision.selected_index == 0
    assert decision.reason == "weak-evidence"


def test_dropped_long_vowel_can_select_different_length_candidate():
    decision = choose_reading(
        ["ホントウワダキアッテ", "ホントワダキアッテ"],
        ["ホントワダキアッテ", "ホントワダキアッテ"],
    )
    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"


def test_marigold_reading_uses_exact_kana_evidence_across_mora_counts():
    decision = choose_reading(
        ["キボーノコーハ", "キボーノヒカリワ", "キボーノヒカルワ"],
        [
            "アシタニワキボーノヒカリワ",
            "キボーノヒカリワソソグ",
        ],
    )

    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"
    assert decision.distances[1] == (0.0, 0.0)


def test_raw_distance_can_select_short_exact_candidate():
    decision = choose_reading(
        ["カキクケコ", "カ"],
        ["カキクケサ", "カキクケサ"],
    )

    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"


def test_raw_distance_does_not_prefer_lower_candidate_normalized_error():
    decision = choose_reading(
        ["カキクケコ", "カキクケサタチツテト"],
        ["カキクケサタチツセソ", "カキクケサタチツセソ"],
    )

    assert sum(decision.distances[0]) < sum(decision.distances[1])
    assert sum(decision.normalized_distances[0]) > sum(decision.normalized_distances[1])
    assert decision.selected_index == 0


def test_kanasim_prefers_phonetically_closer_candidate_when_edit_counts_tie():
    decision = choose_reading(["マ", "ツ"], ["ス", "ス"])

    assert decision.selected_index == 1
    assert decision.distances[1][0] < decision.distances[0][0]


def test_kanasim_accepts_expressive_repeated_long_vowel_marks():
    decision = choose_reading(["キレー", "キロ"], ["キレーー", "キレー"])

    assert decision.distances[0] == (0.0, 0.0)


def test_earlier_dictionary_path_wins_when_nbest_paths_are_one_edit_apart():
    decision = choose_reading(
        [
            "ムネニノコリバナレナイ",
            "ムネニノコリハナレナイ",
            "ムナニノコリハナレナイ",
        ],
        ["ムネニノコリハナレナイ", "ムナニノコリハナレナイ"],
    )
    assert decision.selected_index == 1


def test_build_contexts_packs_nearby_lines_and_skips_abnormal_long_line():
    contexts, assignments = build_kana_contexts(
        [(2.0, 4.0), (5.0, 7.0), (20.0, 22.0), (30.0, 55.0)],
        audio_duration=60.0,
    )
    assert contexts == [
        type(contexts[0])(0.5, 8.5, (0, 1)),
        type(contexts[0])(18.5, 23.5, (2,)),
    ]
    assert assignments == [0, 0, 1, None]
