import pytest

from soramimic_video.audio_melody import MelodyNote
from soramimic_video.mora_align import AlignedMora
from soramimic_video.semantic_lyrics import (
    apply_ctc_support,
    apply_vocal_activity_support,
    coalesce_repeated_suffix_fragments,
    credit_recovery_windows,
    decide_recognized_line,
    lyric_deficit_recoveries,
    non_lyric_template_family,
    normalize_recognized_text,
    normalize_repeated_vocalization,
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


def test_repeated_vocalization_is_capped_to_notes_and_keeps_its_unit():
    line = TranscribedLine(1.0, 5.0, "ダ" * 20)
    notes = [
        MelodyNote(1.0 + index * 0.1, 1.05 + index * 0.1, 60)
        for index in range(4)
    ]

    normalized = normalize_repeated_vocalization(line, notes)

    assert normalized is not None
    assert normalized.line == TranscribedLine(1.0, 5.0, "ダ" * 4)
    assert normalized.unit_moras == ("ダ",)
    assert normalized.original_mora_count == 20
    assert normalized.normalized_mora_count == 4
    assert normalized.note_count == 4


def test_latin_repeated_vocalization_uses_exact_kana_count():
    line = TranscribedLine(1.0, 5.0, "DADADADA")
    notes = [MelodyNote(1.0, 5.0, 60)] * 31

    normalized = normalize_repeated_vocalization(line, notes)

    assert normalized is not None
    assert normalized.line.text == "ダ" * 4
    assert normalized.unit_moras == ("ダ",)
    assert normalized.original_mora_count == 4
    assert normalized.normalized_mora_count == 4
    assert normalized.note_count == 31


def test_short_multimora_pure_vocalization_keeps_its_period():
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


def test_lexical_text_is_not_normalized_as_repeated_vocalization():
    line = TranscribedLine(1.0, 5.0, "wow 君へ")

    assert normalize_repeated_vocalization(
        line, [MelodyNote(1.0, 5.0, 60)]
    ) is None


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
