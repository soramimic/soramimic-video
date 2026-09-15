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


def test_different_mora_count_cannot_be_selected():
    decision = choose_reading(["ヨル", "ヨ"], ["ヨ", "ヨ"])
    assert decision.selected_index == 0
    assert decision.reason == "different-mora-count"


def test_weak_voicing_evidence_keeps_default():
    decision = choose_reading(
        ["キレーゴトジャナイケド", "キレーコトジャナイケド"],
        ["チレーコトジャナイケド", "チレーコトジャナイケド"],
    )
    assert decision.selected_index == 0
    assert decision.reason == "weak-evidence"


def test_dropped_long_vowel_cannot_change_candidate_length():
    decision = choose_reading(
        ["ホントウワダキアッテ", "ホントワダキアッテ"],
        ["ホントワダキアッテ", "ホントワダキアッテ"],
    )
    assert decision.selected_index == 0
    assert decision.reason == "different-mora-count"


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
