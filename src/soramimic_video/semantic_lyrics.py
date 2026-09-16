"""Conservative semantic filtering for automatic lyric recognition."""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .audio_melody import MelodyNote
    from .mora_align import AlignedMora
    from .transcribe import TranscribedLine


DecisionStatus = Literal["accepted", "rejected", "unresolved"]


@dataclass(frozen=True)
class SemanticLyricDecision:
    """A testable recognition decision without changing the recognized text."""

    status: DecisionStatus
    normalized_text: str
    template_family: str | None
    melodic_support: bool
    ctc_support: bool | None = None
    ctc_median_score: float | None = None


_CREDIT_LABEL = r"(?:作詞|作曲|編曲|原作|監督|制作|製作|出演|翻訳|歌唱|動画制作|イラスト)"
_CREDIT_VALUE_LABEL = rf"(?:{_CREDIT_LABEL}|サブタイトル)"
_CREDIT_WITH_VALUE = re.compile(
    rf"^\s*{_CREDIT_VALUE_LABEL}(?:担当|協力|提供|制作|作成)?"
    r"(?:\s*[:：/／|｜]\s*|\s+)"
    r"[0-9a-zA-Zぁ-んァ-ヶ一-龯々〆ヵヶー@._・]{1,32}\s*$"
)
_COMPOUND_CREDIT_WITH_VALUE = re.compile(
    rf"^\s*{_CREDIT_LABEL}"
    rf"(?:\s*[・･/&＆,，、／|｜]\s*{_CREDIT_LABEL})+"
    r"\s*(?:[:：+＋]|\s)\s*"
    r"[0-9a-zA-Zぁ-んァ-ヶ一-龯々〆ヵヶー@._・]{1,32}\s*$"
)
_MIN_MELODY_COVERAGE = 0.25
_MIN_MELODY_SECONDS_PER_CHARACTER = 0.05
# Forced-alignment span scores are acoustic posteriors, not calibrated transcript
# probabilities.  Keep the floor near zero so normal music-degraded alignments are
# not treated as absent.  The median prevents one coincidental kana peak from
# validating a whole line.
MIN_CTC_MEDIAN_SCORE = 0.00075
_TEMPLATES = (
    (
        "closing-greeting",
        re.compile(r"(?:お疲れさま|お疲れ様|おつかれさま)(?:です|でした)?"),
    ),
    (
        "viewing-thanks",
        re.compile(
            r"(?:最後まで)?ご視聴(?:いただき)?"
            r"(?:ありがとう(?:ございます|ございました)|感謝(?:します|いたします)|ください)"
        ),
    ),
    (
        "channel-registration",
        re.compile(
            r"チャンネル登録(?:と高評価)?(?:を)?(?:よろしく)?"
            r"(?:お願いします|お願いいたします|ありがとう(?:ございます|ございました))"
        ),
    ),
    (
        "subtitles",
        re.compile(r"字幕(?:制作|作成|提供|協力|担当)?"),
    ),
    (
        "credits",
        re.compile(rf"{_CREDIT_LABEL}(?:担当|協力|提供|制作|作成)?"),
    ),
)


def normalize_recognized_text(text: str) -> str:
    """Normalize width/case and discard only separators and punctuation."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] not in {"P", "Z"}
    )


def non_lyric_template_family(text: str) -> str | None:
    """Return a narrowly scoped family only when the whole normalized text matches."""
    normalized = normalize_recognized_text(text)
    if not normalized:
        return None
    original = unicodedata.normalize("NFKC", text)
    if _CREDIT_WITH_VALUE.fullmatch(original):
        return "credits"
    if _COMPOUND_CREDIT_WITH_VALUE.fullmatch(original):
        return "credits"
    for family, pattern in _TEMPLATES:
        if pattern.fullmatch(normalized):
            return family
    return None


def interval_has_melodic_support(
    start_sec: float,
    end_sec: float,
    text_length: int,
    notes: list[MelodyNote],
) -> bool:
    """Require enough SheetSage time for both the interval and recognized text."""
    interval_duration = max(0.0, end_sec - start_sec)
    required_duration = min(
        interval_duration * _MIN_MELODY_COVERAGE,
        text_length * _MIN_MELODY_SECONDS_PER_CHARACTER,
    )
    overlap_duration = sum(
        max(0.0, min(end_sec, note.end_sec) - max(start_sec, note.start_sec))
        for note in notes
    )
    return required_duration > 0.0 and overlap_duration >= required_duration


def decide_recognized_line(
    line: TranscribedLine,
    notes: list[MelodyNote],
) -> SemanticLyricDecision:
    """Reject only an exact template match without enough melodic duration."""
    normalized = normalize_recognized_text(line.text)
    family = non_lyric_template_family(line.text)
    supported = interval_has_melodic_support(
        line.start_sec, line.end_sec, len(normalized), notes
    )
    if family is not None and not supported:
        status: DecisionStatus = "rejected"
    elif not supported:
        # Keep real non-melodic voice and expose that its pitch is unresolved.
        status = "unresolved"
    else:
        status = "accepted"
    return SemanticLyricDecision(status, normalized, family, supported)


def apply_ctc_support(
    decision: SemanticLyricDecision,
    line: int,
    aligned: list[AlignedMora],
) -> SemanticLyricDecision:
    """Resolve a melody-supported template using text-conditioned CTC evidence.

    Ordinary recognized text is deliberately outside this gate.  A narrow non-lyric
    template is retained only when its own kana have sustained acoustic support.
    """
    if decision.template_family is None or not decision.melodic_support:
        return decision
    scores = [item.score for item in aligned if item.line == line]
    median_score = statistics.median(scores) if scores else 0.0
    supported = median_score >= MIN_CTC_MEDIAN_SCORE
    return replace(
        decision,
        status="accepted" if supported else "rejected",
        ctc_support=supported,
        ctc_median_score=median_score,
    )
