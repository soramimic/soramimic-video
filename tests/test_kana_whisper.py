from soramimic_video.kana_whisper import (
    build_kana_contexts,
    choose_reading,
    has_dictionary_reading_alternative,
    normalize_kana_evidence,
    propose_dictionary_readings,
)


def test_normalize_kana_evidence_removes_non_kana_and_pronunciation_spelling():
    assert normalize_kana_evidence("何? なにを、みていたの") == "ナニオミテータノ"


def test_local_alignment_recovers_dictionary_reading_missing_from_line_nbest():
    surface = "OK! 竜町独壇場 Listen! Listen!"
    default = "オーケイリューマチドクダンジョーリサンリサン"
    evidence = [
        "レディーフォーマンショーオーケータチマチソクダンジョー",
        "レディースルマショーオーケータツマチドクダンチョー",
    ]

    assert has_dictionary_reading_alternative(surface, default)
    proposals = propose_dictionary_readings(surface, default, evidence)

    assert [proposal.reading for proposal in proposals] == [
        "オーケイタツマチドクダンジョーリサンリサン"
    ]
    assert proposals[0].surface == "竜"
    assert proposals[0].default_reading == "リュー"
    assert proposals[0].alternative_reading == "タツ"
    assert proposals[0].evidence_views == (1,)
    decision = choose_reading([default, proposals[0].reading], evidence)
    assert decision.selected_index == 1
    assert decision.reason == "kana-evidence"


def test_alternate_split_requires_matching_kana_evidence():
    surface = "二人今夜に駆け出してく"
    default = "フタリコンヤニカケダシテク"
    alternative = "フタリイマヨルニカケダシテク"

    assert has_dictionary_reading_alternative(surface, default)
    assert propose_dictionary_readings(surface, default, [default]) == ()
    proposals = propose_dictionary_readings(surface, default, [alternative])
    assert [(proposal.reading, proposal.surface) for proposal in proposals] == [
        (alternative, "今夜"),
    ]
    assert choose_reading([default, proposals[0].reading], [alternative]).selected_index == 1


def test_local_alignment_rejects_dictionary_reading_without_exact_context():
    proposals = propose_dictionary_readings(
        "心に炎を灯して 遠い未来まで",
        "ココロニホノオヲトモシテトーイミライマデ",
        [
            "カラココロニクムラオトボシテトーリミライノアテー",
            "カラココロニムムラオトモシテトーイミライマデー",
        ],
    )

    assert all(proposal.alternative_reading != "ホムラ" for proposal in proposals)


def test_local_alignment_does_not_borrow_matching_sound_across_left_context():
    proposals = propose_dictionary_readings(
        "もっと走る熱いパクスで 思い出を裏切るなら",
        "モットハシルアツイパクスデオモイデヲウラギルナラ",
        [
            "ナガテトビタチモノバシルアツイッパツデオモイデモフラギルナラ",
            "ナガデトータチコトバシルアツネパツデオモイデオーラギルナラ",
        ],
    )

    assert all(proposal.alternative_reading != "バシル" for proposal in proposals)


def test_local_alignment_keeps_consonant_only_ambiguity_closed():
    proposals = propose_dictionary_readings(
        "綺麗事じゃないけど",
        "キレーゴトジャナイケド",
        ["キレーコトジャナイケド", "キレーコトジャナイケド"],
    )

    assert proposals == ()


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


def test_kanasim_accepts_same_vowel_small_kana_in_candidate():
    decision = choose_reading(["デェー", "デス"], ["デー", "デー"])

    assert decision.selected_index == 0
    assert decision.distances[0] == (0.0, 0.0)


def test_unsupported_candidate_is_excluded_without_aborting(monkeypatch):
    from soramimic_video import kana_whisper

    original = kana_whisper._phonetic_substring_distance

    def reject_unknown(needle, haystack):
        if needle == "ヴョ":
            raise ValueError("unsupported test mora")
        return original(needle, haystack)

    monkeypatch.setattr(kana_whisper, "_phonetic_substring_distance", reject_unknown)
    decision = choose_reading(["カ", "ヴョ", "サ"], ["サ", "サ"])

    assert decision.selected_index == 2
    assert decision.reason == "kana-evidence"
    assert decision.distances[1] == ()


def test_unsupported_default_uses_first_supported_candidate(monkeypatch):
    from soramimic_video import kana_whisper

    original = kana_whisper._phonetic_substring_distance

    def reject_unknown(needle, haystack):
        if needle == "ヴョ":
            raise ValueError("unsupported test mora")
        return original(needle, haystack)

    monkeypatch.setattr(kana_whisper, "_phonetic_substring_distance", reject_unknown)
    decision = choose_reading(["ヴョ", "カ", "サ"], ["サ", "サ"])

    assert decision.selected_index == 1
    assert decision.reason == "unsupported-default"


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
