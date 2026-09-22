import pytest

from soramimic_video.audio_melody import MelodyNote
from soramimic_video.mora_align import AlignedMora
from soramimic_video.semantic_lyrics import (
    apply_ctc_support,
    apply_vocal_activity_support,
    coalesce_repeated_suffix_fragments,
    credit_recovery_windows,
    decide_recognized_line,
    decide_recognized_lines,
    duration_repeated_vocalization_candidate,
    has_tandem_repeat_ctc_support,
    has_tandem_repeat_note_support,
    has_tandem_repeated_phrase,
    is_pathological_repeated_vocalization,
    lyric_deficit_recoveries,
    non_lyric_template_family,
    normalize_recognized_text,
    normalize_repeated_vocalization,
    repeated_vocalization_period,
    unowned_note_recovery_windows,
    vocalization_only,
)
from soramimic_video.transcribe import TranscribedLine


def test_template_normalization_is_width_case_and_punctuation_stable():
    assert normalize_recognized_text(" ご視聴、ありがとうございました！ ") == (
        "ご視聴ありがとうございました"
    )
    assert non_lyric_template_family(" ご視聴、ありがとうございました！ ") == (
        "viewing-thanks"
    )
    assert non_lyric_template_family("字幕制作") == "subtitles"
    assert non_lyric_template_family("字幕") == "subtitles"
    assert non_lyric_template_family("作詞 / 山田太郎") == "credits"
    assert non_lyric_template_family("作曲") == "credits"
    assert non_lyric_template_family("作詞・作曲・編曲 初音ミク") == "credits"
    assert non_lyric_template_family("作詞／作曲／編曲＋cosMo＠暴走P") == "credits"
    assert non_lyric_template_family("サブタイトル 山田太郎") == "credits"
    assert non_lyric_template_family("歌唱 初音ミク") == "credits"
    assert non_lyric_template_family("おつかれさまです") == "closing-greeting"
    assert non_lyric_template_family("Instagram & Twitter ホドリ") == (
        "stock-media-credit"
    )
    assert non_lyric_template_family(
        "🐯 Sound Hodori 사운드 호돌이 サウンドゥ ホドリ"
    ) == "stock-media-credit"


def test_repeated_short_suffix_fragment_rejoins_previous_asr_line():
    lines = [
        TranscribedLine(0.0, 2.9, "どんなに長い葛藤も"),
        TranscribedLine(3.0, 4.98, "見たと知れ"),
        TranscribedLine(4.98, 5.90, "残響"),
        TranscribedLine(8.0, 10.9, "かき消して残響"),
    ]

    merged, evidence = coalesce_repeated_suffix_fragments(lines)

    assert [line.text for line in merged] == [
        "どんなに長い葛藤も",
        "見たと知れ残響",
        "かき消して残響",
    ]
    assert evidence[0].left_index == 1
    assert evidence[0].right_index == 2
    assert evidence[0].merged_surface == "見たと知れ残響"


@pytest.mark.parametrize(
    "text",
    [
        "Get it! Get it! Down! Get it! Get it! Down!",
        "君が好き君が好き",
        "この歌を届けるこの歌を届けるよ",
    ],
)
def test_tandem_repeated_phrase_detects_substantial_adjacent_copy(text):
    assert has_tandem_repeated_phrase(text)


@pytest.mark.parametrize(
    "text",
    ["Get it get it down", "もう一度鳴らせ", "君が好き、ずっと好き"],
)
def test_tandem_repeated_phrase_rejects_words_and_nonidentical_lines(text):
    assert not has_tandem_repeated_phrase(text)


def test_tandem_repeat_note_support_requires_detail_gain_and_better_note_fit():
    text = "Get it! Get it! Down! Get it! Get it! Down!"

    assert has_tandem_repeat_note_support(
        text,
        source_mora_count=7,
        recovered_mora_count=14,
        note_count=14,
        median_notes_per_mora=1.0,
    )
    assert not has_tandem_repeat_note_support(
        "Get it get it down",
        source_mora_count=7,
        recovered_mora_count=7,
        note_count=14,
        median_notes_per_mora=1.0,
    )
    assert not has_tandem_repeat_note_support(
        text,
        source_mora_count=7,
        recovered_mora_count=14,
        note_count=7,
        median_notes_per_mora=1.0,
    )


def test_tandem_repeat_ctc_support_requires_retry_to_improve_on_source():
    assert has_tandem_repeat_ctc_support(
        note_support=True,
        source_ctc_median_score=0.00008,
        recovered_ctc_median_score=0.00021,
    )
    assert not has_tandem_repeat_ctc_support(
        note_support=True,
        source_ctc_median_score=0.00080,
        recovered_ctc_median_score=0.00007,
    )
    assert not has_tandem_repeat_ctc_support(
        note_support=False,
        source_ctc_median_score=0.00008,
        recovered_ctc_median_score=0.00021,
    )
    assert not has_tandem_repeat_ctc_support(
        note_support=True,
        source_ctc_median_score=0.0,
        recovered_ctc_median_score=0.0,
    )


def test_short_standalone_line_is_not_merged_without_parallel_suffix():
    lines = [
        TranscribedLine(0.0, 2.0, "君へ歌う"),
        TranscribedLine(2.0, 2.8, "未来"),
        TranscribedLine(3.0, 5.0, "明日へ進む"),
    ]

    merged, evidence = coalesce_repeated_suffix_fragments(lines)

    assert merged == lines
    assert evidence == []


@pytest.mark.parametrize(
    "text",
    [
        "ご視聴ありがとうございました君へ歌う",
        "字幕の向こうで会おう",
        "作詞家になりたい",
        "作詞・作曲・君へ歌う",
        "サブタイトルの向こうへ",
        "歌 君へ届ける",
        "歌 初音ミク",
        "歌",
        "この歌を届ける",
        "Instagram Twitter ホドリ 君へ",
        "おわり",
    ],
)
def test_template_matching_does_not_use_substrings_or_broad_end_rules(text):
    assert non_lyric_template_family(text) is None


def test_contextual_credit_gate_requires_creator_like_value_for_video():
    lines = [
        TranscribedLine(0.0, 2.0, "映像 初音ミク"),
        TranscribedLine(3.0, 5.0, "映像 ABC Studio"),
        TranscribedLine(6.0, 8.0, "映像 南方研究所"),
        TranscribedLine(9.0, 11.0, "映像 美しい未来"),
        TranscribedLine(12.0, 14.0, "映像 未来"),
        TranscribedLine(15.0, 17.0, "映像の中で君を見た"),
    ]

    decisions = decide_recognized_lines(lines, [])

    assert [decision.template_family for decision in decisions] == [
        "credits",
        "credits",
        "credits",
        None,
        None,
        None,
    ]
    assert [decision.status for decision in decisions] == [
        "rejected",
        "rejected",
        "rejected",
        "unresolved",
        "unresolved",
        "unresolved",
    ]


def test_song_label_requires_a_contiguous_confirmed_credit_block():
    lines = [
        TranscribedLine(0.0, 2.0, "歌 初音ミク"),
        TranscribedLine(3.0, 5.0, "ここから歌が始まる"),
    ]

    decisions = decide_recognized_lines(lines, [])

    assert all(decision.template_family is None for decision in decisions)


def test_credit_block_can_begin_with_song_when_video_follows():
    lines = [
        TranscribedLine(0.0, 2.0, "歌 初音ミク"),
        TranscribedLine(2.0, 4.0, "映像 初音ミク"),
    ]

    decisions = decide_recognized_lines(lines, [])

    assert [decision.template_family for decision in decisions] == [
        "credits",
        "credits",
    ]


def test_contextual_credit_block_propagates_across_kick_back_opening():
    lines = [
        TranscribedLine(0.0, 2.0, "作詞・作曲・編曲 初音ミク"),
        TranscribedLine(2.0, 4.0, "歌 初音ミク"),
        TranscribedLine(4.0, 6.0, "映像 初音ミク"),
        TranscribedLine(6.0, 8.0, "映像 初音ミク"),
        TranscribedLine(8.0, 10.0, "ここから歌が始まる"),
        TranscribedLine(10.0, 12.0, "歌 初音ミク"),
    ]

    decisions = decide_recognized_lines(lines, [])

    assert [decision.template_family for decision in decisions] == [
        "credits",
        "credits",
        "credits",
        "credits",
        None,
        None,
    ]
    assert [decision.status for decision in decisions[:4]] == ["rejected"] * 4


def test_confirmed_credit_value_can_supply_entity_evidence_for_song_label():
    lines = [
        TranscribedLine(0.0, 2.0, "制作 美しい未来"),
        TranscribedLine(2.0, 4.0, "歌 美しい未来"),
    ]

    decisions = decide_recognized_lines(lines, [])

    assert [decision.template_family for decision in decisions] == [
        "credits",
        "credits",
    ]


def test_contextual_credit_recovery_uses_the_sequence_classification():
    line = TranscribedLine(0.0, 10.0, "歌 初音ミク")
    notes = [MelodyNote(1.0, 9.0, 60)]

    assert credit_recovery_windows(
        line, notes, template_family="credits"
    ) == [(1.0, 9.0)]


@pytest.mark.parametrize(
    ("notes", "text", "status"),
    [
        ([], "ご視聴ありがとうございました", "rejected"),
        ([MelodyNote(1.2, 1.8, 60)], "ご視聴ありがとうございました", "accepted"),
        ([], "ここは話し声です", "unresolved"),
        ([MelodyNote(1.2, 1.8, 60)], "ここは歌です", "accepted"),
    ],
)
def test_semantic_gate_is_exact_conjunction_and_preserves_nonmelodic_voice(
    notes, text, status,
):
    decision = decide_recognized_line(TranscribedLine(1.0, 2.0, text), notes)
    assert decision.status == status
    assert decision.melodic_support is bool(notes)


def test_vocal_activity_rejects_only_unsupported_ordinary_nonmelodic_text():
    unresolved = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "ここは話し声です"), []
    )
    rejected = apply_vocal_activity_support(
        unresolved,
        supported=False,
        percentile_dbfs=-64.5,
        relative_db=-51.0,
        active_frame_ratio=0.06,
    )

    assert rejected.status == "rejected"
    assert rejected.vocal_activity_support is False
    assert rejected.vocal_activity_percentile_dbfs == -64.5
    assert rejected.vocal_activity_relative_db == -51.0
    assert rejected.vocal_active_frame_ratio == 0.06

    melodic = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "ここは歌です"),
        [MelodyNote(1.0, 2.0, 60)],
    )
    preserved = apply_vocal_activity_support(
        melodic,
        supported=False,
        percentile_dbfs=-80.0,
        relative_db=-60.0,
        active_frame_ratio=0.0,
    )
    assert preserved.status == "accepted"
    assert preserved.vocal_activity_support is None


def test_vocal_activity_preserves_supported_nonmelodic_voice():
    decision = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "ここは話し声です"), []
    )
    retained = apply_vocal_activity_support(
        decision,
        supported=True,
        percentile_dbfs=-22.0,
        relative_db=-7.0,
        active_frame_ratio=0.8,
    )

    assert retained.status == "unresolved"
    assert retained.vocal_activity_support is True


def test_credit_gate_rejects_clearly_insufficient_melody_time():
    text = "作詞・作曲・編曲 初音ミク"
    line = TranscribedLine(1.0, 5.0, text)

    sparse = decide_recognized_line(line, [MelodyNote(1.0, 1.1, 60)])
    supported = decide_recognized_line(line, [MelodyNote(1.0, 1.6, 60)])

    assert sparse.status == "rejected"
    assert not sparse.melodic_support
    assert supported.status == "accepted"
    assert supported.melodic_support


def test_credit_recovery_windows_join_only_substantial_note_islands():
    line = TranscribedLine(0.0, 30.0, "作詞・作曲・編曲 初音ミク")
    notes = [
        MelodyNote(2.0, 2.2, 60),
        MelodyNote(21.27, 24.1, 62),
        MelodyNote(24.95, 28.625, 64),
    ]

    assert credit_recovery_windows(line, notes) == [(21.27, 28.625)]


def test_recovery_windows_do_not_apply_to_ordinary_lyrics():
    line = TranscribedLine(0.0, 10.0, "この歌を届ける")
    assert credit_recovery_windows(line, [MelodyNote(1.0, 9.0, 60)]) == []


def test_recovery_windows_apply_to_exact_non_lyric_template():
    line = TranscribedLine(0.0, 10.0, "Instagram & Twitter ホドリ")
    assert credit_recovery_windows(line, [MelodyNote(1.0, 9.0, 60)]) == [(1.0, 9.0)]


@pytest.mark.parametrize(
    "text",
    [
        "ラララ…",
        "パラ パラ パラー",
        "サラサラ サラサラ サラサラ",
        "ah...",
        "Oh oh",
        "ああ ああ",
    ],
)
def test_vocalization_only_recognizes_repeated_non_lexical_syllables(text):
    assert vocalization_only(text)


@pytest.mark.parametrize("text", ["ほら", "サラサラ", "あの日", "wow 君へ"])
def test_vocalization_only_preserves_lexical_text(text):
    assert not vocalization_only(text)


def test_repeated_vocalization_preserves_observed_attacks_and_keeps_its_unit():
    line = TranscribedLine(1.0, 5.0, "ダ" * 20)
    notes = [
        MelodyNote(1.0 + index * 0.1, 1.05 + index * 0.1, 60)
        for index in range(4)
    ]

    normalized = normalize_repeated_vocalization(line, notes)

    assert normalized is not None
    assert normalized.line == TranscribedLine(1.0, 5.0, "ダ" * 20)
    assert normalized.unit_moras == ("ダ",)
    assert normalized.original_mora_count == 20
    assert normalized.normalized_mora_count == 20
    assert normalized.note_count == 4


def test_repeated_vocalization_period_distinguishes_multi_mora_refrain():
    assert repeated_vocalization_period("ダダダ") == ("ダ",)
    assert repeated_vocalization_period("アイアイア") == ("ア", "イ")
    assert repeated_vocalization_period("君が好き") is None


def test_duration_repeat_duplicates_source_phrase_after_acoustic_family_match():
    source = TranscribedLine(10.0, 22.0, "アイアイア")
    retry = TranscribedLine(10.0, 22.0, "アイ" * 14)

    candidate = duration_repeated_vocalization_candidate(source, [retry], 2)

    assert candidate == TranscribedLine(10.0, 22.0, "アイアイアアイアイア")
    assert duration_repeated_vocalization_candidate(
        source, [TranscribedLine(10.0, 22.0, "ラララ")], 2
    ) is None


def test_latin_repeated_vocalization_does_not_expand_to_note_count():
    line = TranscribedLine(1.0, 5.0, "DADADADA")
    notes = [MelodyNote(1.0, 5.0, 60)] * 31

    normalized = normalize_repeated_vocalization(line, notes)

    assert normalized is not None
    assert normalized.line.text == "ダ" * 4
    assert normalized.unit_moras == ("ダ",)
    assert normalized.original_mora_count == 4
    assert normalized.normalized_mora_count == 4
    assert normalized.note_count == 31


def test_short_multimora_pure_vocalization_preserves_observed_periods():
    line = TranscribedLine(1.0, 5.0, "ダラダラ...")

    normalized = normalize_repeated_vocalization(
        line,
        [MelodyNote(1.0, 5.0, 60)] * 31,
    )

    assert normalized is not None
    assert normalized.line.text == "ダラダラ"
    assert normalized.unit_moras == ("ダ", "ラ")
    assert normalized.original_mora_count == 4
    assert normalized.normalized_mora_count == 4


def test_repeated_vocalization_without_notes_preserves_recognized_count():
    line = TranscribedLine(1.0, 5.0, "ダラダラ")

    normalized = normalize_repeated_vocalization(line, [])

    assert normalized is not None
    assert normalized.line.text == "ダラダラ"
    assert normalized.original_mora_count == 4
    assert normalized.normalized_mora_count == 4
    assert normalized.note_count == 0


def test_lexical_text_is_not_normalized_as_repeated_vocalization():
    line = TranscribedLine(1.0, 5.0, "wow 君へ")

    assert normalize_repeated_vocalization(
        line, [MelodyNote(1.0, 5.0, 60)]
    ) is None


def test_only_runaway_repetition_is_pathological():
    assert not is_pathological_repeated_vocalization(
        TranscribedLine(0.0, 2.0, "la la la"), 3
    )
    assert not is_pathological_repeated_vocalization(
        TranscribedLine(0.0, 10.0, "ラ" * 40), 24
    )
    assert is_pathological_repeated_vocalization(
        TranscribedLine(0.0, 10.0, "ラ" * 100), 24
    )
    assert is_pathological_repeated_vocalization(
        TranscribedLine(0.0, 2.0, "ラ" * 20), 20
    )


def _unowned_correspondence(notes):
    return {
        "note_candidates": [
            {"id": note_id, "start_sec": start, "end_sec": end}
            for note_id, start, end in notes
        ],
        "links": [
            {
                "operation": "note_only",
                "singing_unit_ids": [],
                "note_candidate_ids": [note_id],
            }
            for note_id, _start, _end in notes
        ],
    }


def test_unowned_note_window_rehydrates_short_internal_island():
    notes = []
    for index, start in enumerate((1.0, 1.2, 1.4, 1.6)):
        notes.append((f"left-{index}", start, start + 0.12))
    for index, start in enumerate((2.3, 2.5, 2.7)):
        notes.append((f"middle-{index}", start, start + 0.12))
    for index, start in enumerate((3.4, 3.65, 3.9, 4.15)):
        # The first three establish a short island; the long final note makes the
        # second seed's span and the merged window duration pass the safety gate.
        notes.append((f"right-{index}", start, start + (1.65 if index == 3 else 0.12)))

    windows = unowned_note_recovery_windows(
        _unowned_correspondence(notes), []
    )

    assert len(windows) == 1
    assert windows[0].start_sec == 1.0
    assert windows[0].end_sec == pytest.approx(5.8)
    assert windows[0].note_count == 11
    assert windows[0].seed_note_count == 8
    assert windows[0].note_ids[4:7] == ("middle-0", "middle-1", "middle-2")


def test_unowned_note_window_rejects_overlap_with_retained_transcript():
    notes = [
        (f"n-{index}", index * 0.55, index * 0.55 + 0.5)
        for index in range(9)
    ]

    assert unowned_note_recovery_windows(
        _unowned_correspondence(notes),
        [TranscribedLine(2.0, 2.5, "既存")],
    ) == []


def test_unowned_note_window_keeps_long_prefix_before_retained_transcript():
    notes = [
        (f"n-{index}", index * 0.4, index * 0.4 + 0.35)
        for index in range(14)
    ]

    windows = unowned_note_recovery_windows(
        _unowned_correspondence(notes),
        [TranscribedLine(4.75, 6.0, "既存")],
    )

    assert len(windows) == 1
    assert windows[0].start_sec == 0.0
    assert windows[0].end_sec == pytest.approx(4.75)
    assert windows[0].note_count == 12
    assert windows[0].note_ids[-1] == "n-11"


def test_lyric_deficit_recovery_uses_song_median_and_internal_note_rests():
    lines = [
        TranscribedLine(0.0, 2.0, "通常一"),
        TranscribedLine(3.0, 5.0, "通常二"),
        TranscribedLine(6.0, 8.0, "通常三"),
        TranscribedLine(10.0, 13.0, "不足"),
        TranscribedLine(14.0, 17.0, "ラララ"),
    ]
    notes = []
    for base in (0.0, 3.0, 6.0):
        notes.extend(MelodyNote(base + index * 0.2, base + index * 0.2 + 0.1, 60)
                     for index in range(4))
    notes.extend(MelodyNote(10.0 + index * 0.2, 10.1 + index * 0.2, 62)
                 for index in range(5))
    notes.extend(MelodyNote(11.4 + index * 0.2, 11.5 + index * 0.2, 64)
                 for index in range(5))
    notes.extend(MelodyNote(14.0 + index * 0.2, 14.1 + index * 0.2, 65)
                 for index in range(10))

    recoveries = lyric_deficit_recoveries(lines, [4, 4, 4, 4, 3], notes)

    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery.line == 3
    assert recovery.note_count == 10
    assert recovery.mora_count == 4
    assert recovery.effective_mora_count == 4
    assert recovery.median_notes_per_mora == 1.0
    assert recovery.residual_notes == 6.0
    assert recovery.windows[0] == pytest.approx((10.0, 11.15))
    assert recovery.windows[1] == pytest.approx((11.15, 12.8))


def test_lyric_deficit_counts_latin_words_but_keeps_severe_collapse():
    lines = [
        TranscribedLine(0.0, 2.0, "通常一"),
        TranscribedLine(3.0, 5.0, "通常二"),
        TranscribedLine(6.0, 8.0, "通常三"),
        TranscribedLine(9.0, 12.0, "プロベス I'm the best yeah"),
        TranscribedLine(13.0, 18.0, "Bring Bam Bam"),
    ]
    notes = []
    for base in (0.0, 3.0, 6.0):
        notes.extend(
            MelodyNote(base + index * 0.2, base + index * 0.2 + 0.1, 60)
            for index in range(4)
        )
    notes.extend(
        MelodyNote(9.0 + index * 0.2, 9.1 + index * 0.2, 62)
        for index in range(11)
    )
    notes.extend(
        MelodyNote(13.0 + index * 0.2, 13.1 + index * 0.2, 64)
        for index in range(19)
    )

    recoveries = lyric_deficit_recoveries(lines, [4, 4, 4, 4, 8], notes)

    assert [recovery.line for recovery in recoveries] == [4]
    assert recoveries[0].mora_count == 8
    assert recoveries[0].effective_mora_count == 11


def test_lyric_deficit_retries_only_duration_outlier_multi_mora_vocalization():
    lines = [
        TranscribedLine(0.0, 4.0, "アイアイア"),
        TranscribedLine(5.0, 9.0, "アイアイア"),
        TranscribedLine(10.0, 14.0, "アイアイア"),
        TranscribedLine(15.0, 23.0, "アイアイア"),
    ]
    notes = [
        MelodyNote(line.start_sec + index * 0.2,
                   line.start_sec + index * 0.2 + 0.1, 60)
        for line in lines
        for index in range(5 if line.end_sec - line.start_sec == 4.0 else 20)
    ]

    recoveries = lyric_deficit_recoveries(lines, [5, 5, 5, 5], notes)

    assert [item.line for item in recoveries] == [3]
    assert recoveries[0].note_count == 20
    assert recoveries[0].repeated_surface_median_duration_sec == 4.0
    assert recoveries[0].suggested_repetition_count == 2


def test_lyric_deficit_accepts_one_normal_duration_peer_with_acoustic_retry():
    lines = [
        TranscribedLine(0.0, 7.0, "アイアイア"),
        TranscribedLine(10.0, 38.0, "アイアイア"),
    ]
    notes = [
        *[
            MelodyNote(index * 0.2, index * 0.2 + 0.1, 60)
            for index in range(5)
        ],
        *[
            MelodyNote(10.0 + index * 0.2, 10.1 + index * 0.2, 60)
            for index in range(40)
        ],
    ]

    recoveries = lyric_deficit_recoveries(lines, [5, 5], notes)

    assert [item.line for item in recoveries] == [1]
    assert recoveries[0].repeated_surface_median_duration_sec == 7.0
    assert recoveries[0].suggested_repetition_count == 4


def test_short_melody_time_does_not_reject_ordinary_lyrics():
    decision = decide_recognized_line(
        TranscribedLine(1.0, 5.0, "この歌を届ける"),
        [MelodyNote(1.0, 1.1, 60)],
    )

    assert decision.status == "unresolved"


def test_ctc_support_is_required_only_for_melody_supported_template():
    template = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "作曲"), [MelodyNote(1.0, 2.0, 60)]
    )
    ordinary = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "この歌を届ける"), [MelodyNote(1.0, 2.0, 60)]
    )
    weak = [
        AlignedMora(0, 0, "サ", 1.1, 1.2, 0.0004),
        AlignedMora(0, 1, "ッ", 1.2, 1.3, 0.0006),
        AlignedMora(0, 2, "キョ", 1.3, 1.4, 0.0007),
    ]

    rejected = apply_ctc_support(template, 0, weak)
    untouched = apply_ctc_support(ordinary, 0, weak)

    assert rejected.status == "rejected"
    assert rejected.ctc_support is False
    assert rejected.ctc_median_score == pytest.approx(0.0006)
    assert untouched == ordinary


def test_ctc_support_retains_sung_noncredit_template_when_most_moras_are_supported():
    decision = decide_recognized_line(
        TranscribedLine(1.0, 2.0, "字幕"), [MelodyNote(1.0, 2.0, 60)]
    )
    aligned = [
        AlignedMora(0, 0, "サ", 1.1, 1.2, 0.001),
        AlignedMora(0, 1, "ッ", 1.2, 1.3, 0.0005),
        AlignedMora(0, 2, "キョ", 1.3, 1.4, 0.0009),
    ]

    accepted = apply_ctc_support(decision, 0, aligned)

    assert accepted.status == "accepted"
    assert accepted.ctc_support is True
    assert accepted.ctc_median_score == pytest.approx(0.0009)


def test_credit_is_rejected_even_with_coincidental_ctc_support():
    decision = decide_recognized_line(
        TranscribedLine(0.0, 21.0, "作詞・作曲・編曲 初音ミク"),
        [MelodyNote(9.0, 21.0, 60)],
    )
    aligned = [AlignedMora(0, 0, "サ", 9.1, 9.2, 0.01)]

    rejected = apply_ctc_support(decision, 0, aligned)

    assert rejected.status == "rejected"
    assert rejected.ctc_support is False
    assert rejected.ctc_median_score == pytest.approx(0.01)


def test_stock_media_credit_is_rejected_even_with_coincidental_ctc_support():
    decision = decide_recognized_line(
        TranscribedLine(1.0, 3.0, "Instagram & Twitter ホドリ"),
        [MelodyNote(1.0, 3.0, 60)],
    )
    aligned = [AlignedMora(0, 0, "ホ", 1.1, 1.2, 0.9)]

    rejected = apply_ctc_support(decision, 0, aligned)

    assert rejected.status == "rejected"
    assert rejected.ctc_support is False
    assert rejected.ctc_median_score == pytest.approx(0.9)
