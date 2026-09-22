"""Conservative semantic filtering for automatic lyric recognition."""

from __future__ import annotations

import re
import statistics
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

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


@dataclass(frozen=True)
class RepeatedVocalizationNormalization:
    """A pure ASR vocalization rewritten to a note-matched kana repetition."""

    line: TranscribedLine
    unit_moras: tuple[str, ...]
    original_mora_count: int
    normalized_mora_count: int
    note_count: int


@dataclass(frozen=True)
class UnownedNoteRecoveryWindow:
    """A conservative retry interval made only from Stage 3 note-only links."""

    start_sec: float
    end_sec: float
    note_ids: tuple[str, ...]
    seed_note_count: int

    @property
    def note_count(self) -> int:
        return len(self.note_ids)


_UNOWNED_CLUSTER_MAX_GAP_SEC = 0.32
_UNOWNED_CLUSTER_MIN_NOTES = 4
_UNOWNED_CLUSTER_MIN_SPAN_SEC = 0.6
_UNOWNED_WINDOW_MERGE_GAP_SEC = 2.0
_UNOWNED_WINDOW_MIN_NOTES = 8
_UNOWNED_WINDOW_MIN_SPAN_SEC = 4.0


def unowned_note_recovery_windows(
    correspondence: Mapping[str, Any],
    retained_lines: Sequence[TranscribedLine],
) -> list[UnownedNoteRecoveryWindow]:
    """Find long lyric-free note runs without consuming nearby owned notes.

    Stage 3 is the ownership authority: only ``note_only`` links without singing
    units seed a window.  Short islands establish that a window is coherent, then
    every unowned note inside the merged envelope is restored.  That second pass
    is important for brief internal islands which would otherwise disappear when
    two longer clusters are joined.
    """
    raw_notes = correspondence.get("note_candidates")
    raw_links = correspondence.get("links")
    if not isinstance(raw_notes, list) or not isinstance(raw_links, list):
        raise ValueError("Stage 3対応表のノートまたはリンクが不正です")

    notes_by_id: dict[str, tuple[float, float]] = {}
    for raw in raw_notes:
        if not isinstance(raw, dict):
            continue
        note_id = raw.get("id")
        start = raw.get("start_sec")
        end = raw.get("end_sec")
        if not isinstance(note_id, str) or isinstance(start, bool) or isinstance(end, bool):
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start_sec, end_sec = float(start), float(end)
        if end_sec > start_sec:
            notes_by_id[note_id] = (start_sec, end_sec)

    unowned_ids = {
        note_id
        for raw in raw_links
        if isinstance(raw, dict)
        and raw.get("operation") == "note_only"
        and raw.get("singing_unit_ids") == []
        for note_id in raw.get("note_candidate_ids", [])
        if isinstance(note_id, str) and note_id in notes_by_id
    }
    unowned = sorted(
        ((start, end, note_id) for note_id in unowned_ids
         for start, end in (notes_by_id[note_id],)),
        key=lambda item: (item[0], item[1], item[2]),
    )
    # A note-only run can extend into a later Whisper line even though Stage 3
    # did not assign those tail notes to it.  Remove only the notes that actually
    # overlap retained text; discarding the whole run makes an otherwise safe,
    # long prefix disappear nondeterministically when that later line moves by a
    # few frames between full-song Whisper runs.
    unowned = [
        note
        for note in unowned
        if not any(
            line.start_sec < note[1] and line.end_sec > note[0]
            for line in retained_lines
        )
    ]
    if not unowned:
        return []

    clusters: list[list[tuple[float, float, str]]] = []
    for note in unowned:
        crosses_retained_line = bool(clusters) and any(
            line.start_sec < note[0] and line.end_sec > clusters[-1][-1][1]
            for line in retained_lines
        )
        if (
            not clusters
            or note[0] - clusters[-1][-1][1] > _UNOWNED_CLUSTER_MAX_GAP_SEC
            or crosses_retained_line
        ):
            clusters.append([note])
        else:
            clusters[-1].append(note)
    seeds = [
        cluster
        for cluster in clusters
        if len(cluster) >= _UNOWNED_CLUSTER_MIN_NOTES
        and cluster[-1][1] - cluster[0][0] >= _UNOWNED_CLUSTER_MIN_SPAN_SEC
    ]
    merged: list[list[tuple[float, float, str]]] = []
    for seed in seeds:
        proposed_start = merged[-1][0][0] if merged else seed[0][0]
        crosses_retained_line = any(
            line.start_sec < seed[-1][1] and line.end_sec > proposed_start
            for line in retained_lines
        )
        if (
            not merged
            or seed[0][0] - merged[-1][-1][1] > _UNOWNED_WINDOW_MERGE_GAP_SEC
            or crosses_retained_line
        ):
            merged.append(list(seed))
        else:
            merged[-1].extend(seed)

    windows = []
    for seed_group in merged:
        start_sec, end_sec = seed_group[0][0], seed_group[-1][1]
        rehydrated = [
            note for note in unowned
            if note[0] >= start_sec and note[1] <= end_sec
        ]
        if (
            len(rehydrated) < _UNOWNED_WINDOW_MIN_NOTES
            or end_sec - start_sec < _UNOWNED_WINDOW_MIN_SPAN_SEC
        ):
            continue
        windows.append(UnownedNoteRecoveryWindow(
            start_sec,
            end_sec,
            tuple(note[2] for note in rehydrated),
            len(seed_group),
        ))
    return windows


_CREDIT_LABEL = r"(?:作詞|作曲|編曲|原作|監督|制作|製作|出演|翻訳|歌唱|動画制作|イラスト)"
_CREDIT_VALUE_LABEL = rf"(?:{_CREDIT_LABEL}|サブタイトル)"
_CREDIT_WITH_VALUE = re.compile(
    rf"^\s*{_CREDIT_VALUE_LABEL}(?:担当|協力|提供|制作|作成)?"
    r"(?:\s*[:：/／|｜]\s*|\s+)"
    r"(?P<value>[0-9a-zA-Zぁ-んァ-ヶ一-龯々〆ヵヶー@._・]{1,32})\s*$"
)
_COMPOUND_CREDIT_WITH_VALUE = re.compile(
    rf"^\s*{_CREDIT_LABEL}"
    rf"(?:\s*[・･/&＆,，、／|｜]\s*{_CREDIT_LABEL})+"
    r"\s*(?:[:：+＋]|\s)\s*"
    r"(?P<value>[0-9a-zA-Zぁ-んァ-ヶ一-龯々〆ヵヶー@._・]{1,32})\s*$"
)
_CONTEXTUAL_CREDIT_WITH_VALUE = re.compile(
    r"^\s*(?P<label>映像|歌)(?:担当|制作|作成)?"
    r"(?:\s*[:：/／|｜]\s*|\s+)"
    r"(?P<value>[0-9a-zA-Zぁ-んァ-ヶ一-龯々〆ヵヶー"
    r"@._・+＋#＃&＆*＊\-\s]{1,48}?)\s*$"
)
_CREDIT_BLOCK_MAX_GAP_SEC = 0.5
_CREDIT_ENTITY_ORGANIZATION_SUFFIX = re.compile(
    r"(?:研究所|スタジオ|工房|プロジェクト|チーム|制作室|映像部)$"
)
_credit_entity_tagger: Any = None
_credit_entity_tagger_lock = threading.Lock()
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


_LATIN_VOCALIZATION_TOKEN = re.compile(
    r"wow|la|na|da|fa|ha|ya|a+h*|o+h*|u+h*"
)


def _latin_vocalization_moras(text: str) -> list[str] | None:
    moras: list[str] = []
    cursor = 0
    while cursor < len(text):
        match = _LATIN_VOCALIZATION_TOKEN.match(text, cursor)
        if match is None:
            return None
        token = match.group()
        if token == "wow":
            moras.extend(("ワ", "ウ"))
        elif token == "la":
            moras.append("ラ")
        elif token == "na":
            moras.append("ナ")
        elif token == "da":
            moras.append("ダ")
        elif token == "fa":
            moras.append("ファ")
        elif token == "ha":
            moras.append("ハ")
        elif token == "ya":
            moras.append("ヤ")
        elif token.startswith("a"):
            moras.append("ア")
        elif token.startswith("o"):
            moras.append("オ")
        else:
            moras.append("ウ")
        cursor = match.end()
    return moras


def _minimal_vocalization_period(moras: list[str]) -> tuple[str, ...] | None:
    """Return a 1--3-mora period after the narrow vocalization-only gate."""
    for width in range(1, min(3, len(moras) // 2) + 1):
        if all(mora == moras[index % width] for index, mora in enumerate(moras)):
            return tuple(moras[:width])
    return None


def is_pathological_repeated_vocalization(
    line: TranscribedLine,
    note_count: int,
) -> bool:
    """Flag only runaway periodic ASR, not an ordinary short repetition."""
    if note_count < 0:
        raise ValueError("note_count must be non-negative")
    normalized = normalize_recognized_text(line.text).replace("ー", "")
    if not normalized or not vocalization_only(line.text):
        return False
    if normalized.isascii():
        moras = _latin_vocalization_moras(normalized)
    elif re.fullmatch(r"[ぁ-んァ-ヶ]+", normalized):
        from .kana import split_moras

        moras = split_moras(normalized)
    else:
        return False
    if not moras or _minimal_vocalization_period(moras) is None:
        return False
    duration = max(1e-6, line.end_sec - line.start_sec)
    return (
        len(moras) > max(64, note_count * 2)
        or len(moras) / duration > 8.0
    )


def normalize_repeated_vocalization(
    line: TranscribedLine,
    notes: list[MelodyNote],
) -> RepeatedVocalizationNormalization | None:
    """Canonicalize a pure repetition without inventing one attack per note.

    Whisper can emit hundreds of repeated syllables for a short bounded interval.
    It can also collapse several audible attacks into only a few syllables.  The
    A pitch change is not proof of a new syllable, while a repeated syllable can
    also reattack without a pitch change.  SheetSage notes therefore must not set
    the mora count.  Preserve Whisper's observed count and let CTC/Stage 3 decide
    attacks and melisma.  Latin vocalizations are converted directly to kana so
    generic English reading heuristics cannot collapse or spell out the repetition.
    """
    if not vocalization_only(line.text):
        return None
    normalized = normalize_recognized_text(line.text).replace("ー", "")
    if not normalized:
        return None
    if normalized.isascii():
        moras = _latin_vocalization_moras(normalized)
    elif re.fullmatch(r"[ぁ-んァ-ヶ]+", normalized):
        from .kana import split_moras

        moras = split_moras(normalized)
    else:
        return None
    if not moras:
        return None
    period = _minimal_vocalization_period(moras)
    if period is None:
        return None
    note_count = sum(
        line.start_sec <= (note.start_sec + note.end_sec) / 2 < line.end_sec
        for note in notes
    )
    normalized_moras = list(moras)
    normalized_line = type(line)(
        line.start_sec,
        line.end_sec,
        "".join(normalized_moras),
    )
    return RepeatedVocalizationNormalization(
        line=normalized_line,
        unit_moras=period,
        original_mora_count=len(moras),
        normalized_mora_count=len(normalized_moras),
        note_count=note_count,
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


def _credit_value_parts_of_speech(value: str) -> list[tuple[str, str, str]]:
    """Return UniDic POS fields used only by the conservative credit-value gate."""
    global _credit_entity_tagger
    with _credit_entity_tagger_lock:
        if _credit_entity_tagger is None:
            import MeCab
            import unidic_lite

            _credit_entity_tagger = MeCab.Tagger("-d " + unidic_lite.DICDIR)
        node = _credit_entity_tagger.parseToNode(value)
        parts = []
        while node:
            if node.surface:
                fields = node.feature.split(",")
                fields.extend(["*"] * (3 - len(fields)))
                parts.append((fields[0], fields[1], fields[2]))
            node = node.next
    return parts


def _credit_value_is_entity_like(value: str) -> bool:
    """Require creator-name evidence instead of treating arbitrary text as a credit."""
    compact = re.sub(r"\s+", "", value)
    if not compact or len(compact) > 32:
        return False
    parts = _credit_value_parts_of_speech(compact)
    if any(
        major in {"動詞", "形容詞", "助詞", "助動詞", "代名詞"}
        for major, _minor, _detail in parts
    ):
        return False
    if any(
        major == "名詞" and minor == "固有名詞"
        for major, minor, _detail in parts
    ):
        return True
    if _CREDIT_ENTITY_ORGANIZATION_SUFFIX.search(compact):
        return True
    # Latin handles and mixed-script creator names are common and UniDic normally
    # labels them as ordinary nouns or unknown words.  An explicit label/value
    # layout plus Latin letters is narrow enough for the standalone 映像 case.
    return bool(re.search(r"[A-Za-z]", compact))


def _contextual_credit_candidate(text: str) -> tuple[str, str] | None:
    original = unicodedata.normalize("NFKC", text)
    match = _CONTEXTUAL_CREDIT_WITH_VALUE.fullmatch(original)
    if match is None:
        return None
    return match.group("label"), match.group("value").strip()


def _confirmed_credit_value(text: str) -> str | None:
    original = unicodedata.normalize("NFKC", text)
    for pattern in (_CREDIT_WITH_VALUE, _COMPOUND_CREDIT_WITH_VALUE):
        match = pattern.fullmatch(original)
        if match is not None:
            return normalize_recognized_text(match.group("value"))
    return None


def _credit_lines_are_adjacent(left: TranscribedLine, right: TranscribedLine) -> bool:
    boundary_delta = right.start_sec - left.end_sec
    return (
        right.start_sec >= left.start_sec
        and abs(boundary_delta) <= _CREDIT_BLOCK_MAX_GAP_SEC
    )


def contextual_non_lyric_template_families(
    lines: Sequence[TranscribedLine],
) -> list[str | None]:
    """Classify soft credit labels using entity evidence and a contiguous block.

    ``映像`` may anchor a block when its value independently looks like a creator.
    The much more lyric-like ``歌`` is accepted only next to an already confirmed
    credit.  Ordinary text breaks propagation, so this does not become a generic
    duplicate-text or opening-position suppression rule.
    """
    families = [non_lyric_template_family(line.text) for line in lines]
    candidates = [_contextual_credit_candidate(line.text) for line in lines]
    confirmed_values = {
        value
        for line in lines
        if (value := _confirmed_credit_value(line.text)) is not None
    }
    entity_like = [
        candidate is not None
        and (
            _credit_value_is_entity_like(candidate[1])
            or normalize_recognized_text(candidate[1]) in confirmed_values
        )
        for candidate in candidates
    ]

    for index, candidate in enumerate(candidates):
        if candidate is not None and candidate[0] == "映像" and entity_like[index]:
            families[index] = "credits"

    changed = True
    while changed:
        changed = False
        for index, candidate in enumerate(candidates):
            if candidate is None or families[index] is not None or not entity_like[index]:
                continue
            adjacent_credit = (
                index > 0
                and families[index - 1] == "credits"
                and _credit_lines_are_adjacent(lines[index - 1], lines[index])
            ) or (
                index + 1 < len(lines)
                and families[index + 1] == "credits"
                and _credit_lines_are_adjacent(lines[index], lines[index + 1])
            )
            if adjacent_credit:
                families[index] = "credits"
                changed = True
    return families


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
    *,
    template_family: str | None = None,
) -> list[tuple[float, float]]:
    """Return substantial SheetSage singing islands inside a template candidate.

    This deliberately applies only to exact non-lyric templates.  The returned hard
    bounds let a second Whisper pass hear the singing without the long silent or
    instrumental context that can induce a credit hallucination.
    """
    if template_family is None:
        template_family = non_lyric_template_family(line.text)
    if template_family is None:
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


def decide_recognized_lines(
    lines: Sequence[TranscribedLine],
    notes: list[MelodyNote],
) -> list[SemanticLyricDecision]:
    """Decide a transcript together so contiguous soft credit labels have context."""
    families = contextual_non_lyric_template_families(lines)
    decisions = []
    for line, family in zip(lines, families, strict=True):
        decision = decide_recognized_line(line, notes)
        if family != decision.template_family:
            decision = replace(decision, template_family=family)
            if family is not None and not decision.melodic_support:
                decision = replace(decision, status="rejected")
        decisions.append(decision)
    return decisions


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
