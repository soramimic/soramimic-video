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
    vocal_activity_support: bool | None = None
    vocal_activity_percentile_dbfs: float | None = None
    vocal_activity_relative_db: float | None = None
    vocal_active_frame_ratio: float | None = None


@dataclass(frozen=True)
class LyricDeficitRecovery:
    """A retained Whisper line whose melody carries substantially more detail."""

    line: int
    windows: tuple[tuple[float, float], ...]
    note_count: int
    mora_count: int
    effective_mora_count: int
    median_notes_per_mora: float
    residual_notes: float


@dataclass(frozen=True)
class RecognitionBoundaryMerge:
    """A short ASR suffix rejoined using repetition elsewhere in the song."""

    left_index: int
    right_index: int
    left_surface: str
    right_surface: str
    merged_surface: str


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
_RECOVERY_MAX_NOTE_GAP_SEC = 1.0
_RECOVERY_MIN_SPAN_SEC = 1.5
_RECOVERY_MIN_MELODY_SEC = 0.5
_DEFICIT_MIN_NOTES = 7
_DEFICIT_MIN_NOTES_PER_MORA = 1.5
_DEFICIT_MIN_RESIDUAL_NOTES = 4.0
_DEFICIT_SPLIT_NOTE_GAP_SEC = 0.32
_DEFICIT_MIN_GROUP_NOTES = 4
_DEFICIT_MIN_GROUP_SPAN_SEC = 0.6
_DEFICIT_WINDOW_PADDING_SEC = 0.5
_BOUNDARY_FRAGMENT_MAX_SEC = 1.25
_BOUNDARY_FRAGMENT_MAX_CHARS = 4
_BOUNDARY_ADJACENCY_SEC = 0.15
_BOUNDARY_COMBINED_MAX_SEC = 6.0
# Forced-alignment span scores are acoustic posteriors, not calibrated transcript
# probabilities.  Keep the floor near zero so normal music-degraded alignments are
# not treated as absent.  The median prevents one coincidental kana peak from
# validating a whole line.
MIN_CTC_MEDIAN_SCORE = 0.00075
_ALWAYS_REJECT_TEMPLATE_FAMILIES = frozenset({"credits", "stock-media-credit"})
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
        "stock-media-credit",
        re.compile(
            r"(?:🐯?soundhodori사운드호돌이サウンドゥ?ホドリ|"
            r"instagramtwitterホドリ)"
        ),
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


def coalesce_repeated_suffix_fragments(
    lines: list[TranscribedLine],
) -> tuple[list[TranscribedLine], list[RecognitionBoundaryMerge]]:
    """Rejoin a short contiguous suffix when another refrain keeps it attached.

    Whisper sometimes emits the final word of a lyric line as its own segment.  A
    timing-only merge would also collapse legitimate short responses, so require
    song-internal structural evidence: the same short Japanese text must occur as
    the suffix of another, longer ASR line, and the combined phrase lengths must be
    comparable.  This uses no supplied or evaluation lyrics.
    """
    normalized = [normalize_recognized_text(line.text) for line in lines]
    candidates: set[int] = set()
    for index in range(1, len(lines)):
        line = lines[index]
        previous = lines[index - 1]
        fragment = normalized[index]
        previous_text = normalized[index - 1]
        if not re.fullmatch(
            rf"[ぁ-んァ-ヶ一-龯々〆ヵヶー]{{2,{_BOUNDARY_FRAGMENT_MAX_CHARS}}}",
            fragment,
        ):
            continue
        if line.end_sec - line.start_sec > _BOUNDARY_FRAGMENT_MAX_SEC:
            continue
        if abs(line.start_sec - previous.end_sec) > _BOUNDARY_ADJACENCY_SEC:
            continue
        if line.end_sec - previous.start_sec > _BOUNDARY_COMBINED_MAX_SEC:
            continue
        combined_length = len(previous_text) + len(fragment)
        has_parallel_suffix = any(
            other_index not in {index - 1, index}
            and len(other_text) > len(fragment)
            and other_text.endswith(fragment)
            and 0.67 <= combined_length / len(other_text) <= 1.5
            for other_index, other_text in enumerate(normalized)
        )
        if has_parallel_suffix:
            candidates.add(index)

    merged_lines: list[TranscribedLine] = []
    merges: list[RecognitionBoundaryMerge] = []
    index = 0
    while index < len(lines):
        if index + 1 in candidates:
            left = lines[index]
            right = lines[index + 1]
            separator = (
                " "
                if left.text[-1:].isascii() and right.text[:1].isascii()
                else ""
            )
            surface = left.text.rstrip() + separator + right.text.lstrip()
            merged_lines.append(
                type(left)(left.start_sec, right.end_sec, surface)
            )
            merges.append(
                RecognitionBoundaryMerge(
                    left_index=index,
                    right_index=index + 1,
                    left_surface=left.text,
                    right_surface=right.text,
                    merged_surface=surface,
                )
            )
            index += 2
            continue
        merged_lines.append(lines[index])
        index += 1
    return merged_lines, merges


def vocalization_only(text: str) -> bool:
    """Return true for short repeated non-lexical syllables such as ``la``/``ah``."""
    normalized = normalize_recognized_text(text)
    if not normalized:
        return False
    compact = normalized.replace("ー", "")
    if all(character.isascii() or "ぁ" <= character <= "ヶ" for character in compact):
        for width in range(1, min(3, len(compact) // 2) + 1):
            if width > 1 and len(compact) < width * 3:
                continue
            if all(character == compact[index % width] for index, character in enumerate(compact)):
                return True
    tokens = re.findall(r"[a-z]+|[ぁ-んァ-ヶー]+", normalized)
    if not tokens or "".join(tokens) != normalized:
        return False
    latin = re.compile(r"(?:(?:a+h*|o+h*|u+h*|la|na|da|fa|ha|ya|wow))+")
    kana = frozenset("あぁアァいぃイィうぅウゥえぇエェおぉオォらラなナだダふフはハやヤわワー")
    return all(
        bool(latin.fullmatch(token)) if token.isascii()
        else all(character in kana for character in token)
        for token in tokens
    )


def lyric_deficit_recoveries(
    lines: list[TranscribedLine],
    mora_counts: list[int],
    notes: list[MelodyNote],
) -> list[LyricDeficitRecovery]:
    """Rank conservative local retries from notes missing matching lyric detail.

    The song-local median absorbs normal note splitting and melisma.  Retry windows
    are then divided at substantial internal SheetSage rests and clipped to the
    source Whisper line, so accepted replacements cannot duplicate adjacent lines.
    """
    if len(lines) != len(mora_counts):
        raise ValueError("mora_counts must contain one value per lyric line")
    rows: list[tuple[int, TranscribedLine, int, int, list[MelodyNote]]] = []
    for index, (line, mora_count) in enumerate(zip(lines, mora_counts, strict=True)):
        line_notes = sorted(
            (
                note
                for note in notes
                if line.start_sec
                <= (note.start_sec + note.end_sec) / 2
                < line.end_sec
            ),
            key=lambda note: note.start_sec,
        )
        # Some reading backends omit unknown Latin tokens.  Counting each Latin
        # word once is deliberately modest, but prevents ordinary mixed-language
        # lyrics from looking like wholesale omissions.  A severe collapse such
        # as one short phrase covering several refrains still clears the gate.
        effective_mora_count = mora_count + len(re.findall(r"[A-Za-z]+", line.text))
        rows.append((index, line, mora_count, effective_mora_count, line_notes))
    ratios = [
        len(line_notes) / effective_mora_count
        for _index, _line, _mora_count, effective_mora_count, line_notes in rows
        if effective_mora_count >= 4 and len(line_notes) >= 2
    ]
    if not ratios:
        return []
    median_ratio = statistics.median(ratios)
    recoveries = []
    for index, line, mora_count, effective_mora_count, line_notes in rows:
        note_count = len(line_notes)
        ratio = note_count / max(effective_mora_count, 1)
        residual = note_count - median_ratio * effective_mora_count
        if (
            vocalization_only(line.text)
            or note_count < _DEFICIT_MIN_NOTES
            or ratio < _DEFICIT_MIN_NOTES_PER_MORA
            or residual < _DEFICIT_MIN_RESIDUAL_NOTES
        ):
            continue
        groups: list[list[MelodyNote]] = []
        for note in line_notes:
            if (
                not groups
                or note.start_sec - groups[-1][-1].end_sec
                > _DEFICIT_SPLIT_NOTE_GAP_SEC
            ):
                groups.append([note])
            else:
                groups[-1].append(note)
        groups = [
            group
            for group in groups
            if len(group) >= _DEFICIT_MIN_GROUP_NOTES
            and group[-1].end_sec - group[0].start_sec
            >= _DEFICIT_MIN_GROUP_SPAN_SEC
        ]
        if not groups:
            continue
        windows = []
        for group_index, group in enumerate(groups):
            start = max(line.start_sec, group[0].start_sec - _DEFICIT_WINDOW_PADDING_SEC)
            end = min(line.end_sec, group[-1].end_sec + _DEFICIT_WINDOW_PADDING_SEC)
            if group_index:
                previous = groups[group_index - 1]
                boundary = (previous[-1].end_sec + group[0].start_sec) / 2
                start = max(start, boundary)
            if group_index + 1 < len(groups):
                following = groups[group_index + 1]
                boundary = (group[-1].end_sec + following[0].start_sec) / 2
                end = min(end, boundary)
            if end > start:
                windows.append((start, end))
        if not windows:
            continue
        recoveries.append(
            LyricDeficitRecovery(
                line=index,
                windows=tuple(windows),
                note_count=note_count,
                mora_count=mora_count,
                effective_mora_count=effective_mora_count,
                median_notes_per_mora=median_ratio,
                residual_notes=residual,
            )
        )
    return recoveries


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


def credit_recovery_windows(
    line: TranscribedLine,
    notes: list[MelodyNote],
) -> list[tuple[float, float]]:
    """Return substantial SheetSage singing islands inside a template candidate.

    This deliberately applies only to exact non-lyric templates.  The returned hard
    bounds let a second Whisper pass hear the singing without the long silent or
    instrumental context that can induce a credit hallucination.
    """
    if non_lyric_template_family(line.text) is None:
        return []
    clipped = [
        (max(line.start_sec, note.start_sec), min(line.end_sec, note.end_sec))
        for note in notes
        if note.end_sec > line.start_sec and note.start_sec < line.end_sec
    ]
    clipped = [(start, end) for start, end in clipped if end > start]
    if not clipped:
        return []
    clipped.sort()
    islands: list[tuple[float, float, float]] = []
    start, end = clipped[0]
    melody_seconds = end - start
    for note_start, note_end in clipped[1:]:
        if note_start - end <= _RECOVERY_MAX_NOTE_GAP_SEC:
            melody_seconds += max(0.0, note_end - max(note_start, end))
            end = max(end, note_end)
        else:
            islands.append((start, end, melody_seconds))
            start, end = note_start, note_end
            melody_seconds = end - start
    islands.append((start, end, melody_seconds))
    return [
        (start, end)
        for start, end, melody_seconds in islands
        if end - start >= _RECOVERY_MIN_SPAN_SEC
        and melody_seconds >= _RECOVERY_MIN_MELODY_SEC
    ]


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


def apply_vocal_activity_support(
    decision: SemanticLyricDecision,
    *,
    supported: bool,
    percentile_dbfs: float,
    relative_db: float,
    active_frame_ratio: float,
) -> SemanticLyricDecision:
    """Reject only unresolved ordinary text from a mostly silent vocal stem.

    Melody-supported lines remain governed by SheetSage, while exact non-lyric
    templates retain their stricter semantic/CTC handling. The energy measurement
    is therefore an additional guard for the ordinary no-melody case, not a
    replacement for either existing signal.
    """
    applies = decision.status == "unresolved" and decision.template_family is None
    return replace(
        decision,
        status="rejected" if applies and not supported else decision.status,
        vocal_activity_support=supported if applies else None,
        vocal_activity_percentile_dbfs=percentile_dbfs,
        vocal_activity_relative_db=relative_db,
        vocal_active_frame_ratio=active_frame_ratio,
    )


def apply_ctc_support(
    decision: SemanticLyricDecision,
    line: int,
    aligned: list[AlignedMora],
) -> SemanticLyricDecision:
    """Resolve a melody-supported template using text-conditioned CTC evidence.

    Ordinary recognized text is deliberately outside this gate.  Generic stock
    phrases may be retained when their own kana have sustained acoustic support,
    but exact credit templates are a strong Whisper prior and always go through the
    bounded recovery path instead of trusting a coincidental forced alignment.
    """
    if decision.template_family is None or not decision.melodic_support:
        return decision
    scores = [item.score for item in aligned if item.line == line]
    median_score = statistics.median(scores) if scores else 0.0
    supported = (
        decision.template_family not in _ALWAYS_REJECT_TEMPLATE_FAMILIES
        and median_score >= MIN_CTC_MEDIAN_SCORE
    )
    return replace(
        decision,
        status="accepted" if supported else "rejected",
        ctc_support=supported,
        ctc_median_score=median_score,
    )
