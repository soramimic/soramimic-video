import pytest

from soramimic_video.audio_melody import MelodyNote
from soramimic_video.semantic_lyrics import (
    decide_recognized_line,
    non_lyric_template_family,
    normalize_recognized_text,
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


@pytest.mark.parametrize(
    "text",
    [
        "ご視聴ありがとうございました君へ歌う",
        "字幕の向こうで会おう",
        "作詞家になりたい",
        "作詞・作曲・君へ歌う",
        "この歌を届ける",
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


def test_credit_gate_rejects_clearly_insufficient_melody_time():
    text = "作詞・作曲・編曲 初音ミク"
    line = TranscribedLine(1.0, 5.0, text)

    sparse = decide_recognized_line(line, [MelodyNote(1.0, 1.1, 60)])
    supported = decide_recognized_line(line, [MelodyNote(1.0, 1.6, 60)])

    assert sparse.status == "rejected"
    assert not sparse.melodic_support
    assert supported.status == "accepted"
    assert supported.melodic_support


def test_short_melody_time_does_not_reject_ordinary_lyrics():
    decision = decide_recognized_line(
        TranscribedLine(1.0, 5.0, "この歌を届ける"),
        [MelodyNote(1.0, 1.1, 60)],
    )

    assert decision.status == "unresolved"
