"""Derive reversible conversion phrases from supplied lyrics and existing timing."""

from __future__ import annotations

import copy
import logging
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Any, Literal

from .align import _pron_normalize
from .kana import split_fine_moras
from .project import Line, Project
from .ruby import has_ruby

logger = logging.getLogger(__name__)

# These are soft limits: preserving a word or an acoustic unit takes precedence.
MAX_MORAS = 32
MAX_SECONDS = 10.0
MIN_MORAS = 6
MIN_SECONDS = 1.2
REST_SECONDS = 0.35
WHISPER_SNAP_SECONDS = 0.8


@dataclass
class _Boundary:
    text: int
    mora: int
    left_end: float
    right_start: float
    whisper: bool = False

    @property
    def rest(self) -> float:
        return max(0.0, self.right_start - self.left_end)


def _surface_boundaries(text: str, kana: str) -> list[tuple[int, int]]:
    """Map bunsetsu ends to exact reading boundaries; never interpolate a reading.

    Spelling and whitespace are retained by slicing the input. A changed reading
    may map at the ends of an edit, but a boundary inside that edit is ambiguous.
    Ruby markup is left intact when its surface cannot be mapped safely.
    """
    if has_ruby(text):
        return []
    try:
        from jphrase import PhraseSplitter

        parts = PhraseSplitter(
            consider_non_independent_nouns_and_verbs_as_breaks=False,
        ).split_text(text, output_type="concatenated")
    except (ImportError, RuntimeError):
        logger.warning("文節解析を利用できないため歌詞の自動分割を省略しました")
        return []
    offsets: list[int] = []
    readings = []
    cursor = 0
    for part in parts:
        surface = part["surface_form"]
        start = text.find(surface, cursor)
        if not surface or start < 0 or text[cursor:start].strip():
            return []
        # Leading whitespace belongs to the preceding phrase, except at offset 0.
        if offsets:
            offsets[-1] = start
        cursor = start + len(surface)
        offsets.append(cursor)
        readings.append(_pron_normalize(part["pronunciation"]))
    if not offsets or text[cursor:].strip():
        return []
    source = "".join(readings)
    moras = split_fine_moras(kana)
    target = _pron_normalize(kana)
    if not source or not target:
        return []
    # Normalizing each prefix retains context for long-vowel normalization.
    mora_offsets = {len(_pron_normalize("".join(moras[:i]))): i
                    for i in range(len(moras) + 1)}
    matcher = SequenceMatcher(None, source, target, autojunk=False)
    if matcher.ratio() < .6:
        return []
    matches: dict[int, set[int]] = {}
    for tag, a, b, c, d in matcher.get_opcodes():
        for left, right in ((a, c), (b, d)):
            matches.setdefault(left, set()).add(right)
        if tag == "equal":
            for i in range(b - a + 1):
                matches.setdefault(a + i, set()).add(c + i)
    result = []
    reading_cursor = 0
    # Brackets and their contents form one surface unit, including annotations.
    depth = 0
    protected = set()
    for i, char in enumerate(text):
        if char in "（([［「『【《":
            depth += 1
        if depth:
            protected.add(i + 1)
        if char in "）)]］」』】》":
            depth = max(0, depth - 1)
            if not depth:
                protected.discard(i + 1)
    for offset, reading in zip(offsets[:-1], readings[:-1], strict=True):
        reading_cursor += len(reading)
        targets = matches.get(reading_cursor, set())
        if len(targets) != 1 or offset in protected:
            continue
        mora = mora_offsets.get(next(iter(targets)))
        if mora is not None and 0 < mora < len(moras):
            result.append((offset, mora))
    return result


def _line_boundaries(
    project: Project, line: Line, canonical: dict[str, Any],
    recognized: list[dict[str, Any]],
) -> list[_Boundary]:
    layers = project.lyric_layers
    assert layers is not None
    ids = canonical["mora_ids"]
    index = {mid: i for i, mid in enumerate(ids)}
    notes_by_mora: list[list[int]] = [[] for _ in ids]
    forbidden: set[int] = set()
    for unit in layers["performed"]:
        positions = [index[mid] for mid in unit["mora_ids"] if mid in index]
        if positions:
            forbidden.update(range(min(positions) + 1, max(positions) + 1))
    for nid in line.note_ids:
        slot = layers["synthesis_plan"][nid]
        positions = [index[mid] for mid in slot["mora_ids"]]
        for pos in positions:
            notes_by_mora[pos].append(nid)
        forbidden.update(range(min(positions) + 1, max(positions) + 1))
    candidates = []
    for text_offset, mora in _surface_boundaries(canonical["text"], canonical["kana"]):
        if mora in forbidden or not notes_by_mora[mora - 1] or not notes_by_mora[mora]:
            continue
        left_end = max(project.notes[n].end_sec for n in notes_by_mora[mora - 1])
        right_start = min(project.notes[n].start_sec for n in notes_by_mora[mora])
        if right_start < left_end - 1e-6:
            continue
        candidates.append(_Boundary(text_offset, mora, left_end, right_start))
    # Recognition supplies timing hints only; its text never replaces input text.
    start, end = project.line_time_range(line)
    for before, after in pairwise(recognized):
        a, b = before.get("end_sec"), after.get("start_sec")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        if not start < a <= b < end or not candidates:
            continue

        def distance(candidate: _Boundary, a: float = a, b: float = b) -> float:
            return max(a - candidate.right_start, candidate.left_end - b, 0.0)

        nearest = min(candidates, key=lambda c: (distance(c), -c.rest, c.mora))
        if distance(nearest) <= WHISPER_SNAP_SECONDS:
            nearest.whisper = True
    return candidates


def _choose_boundaries(
    candidates: list[_Boundary], start: float, end: float, count: int,
    text_length: int, strategy: Literal["hybrid", "whisper", "rest"],
) -> list[_Boundary]:
    if count <= MAX_MORAS and end - start <= MAX_SECONDS:
        return []
    edges = [_Boundary(0, 0, start, start), _Boundary(text_length, count, end, end)]

    def fits(candidate: _Boundary, left: _Boundary, right: _Boundary) -> bool:
        return (candidate.mora - left.mora >= MIN_MORAS
                and right.mora - candidate.mora >= MIN_MORAS
                and candidate.left_end - left.right_start >= MIN_SECONDS
                and right.left_end - candidate.right_start >= MIN_SECONDS)

    preferred = [c for c in candidates if
                 (strategy != "rest" and c.whisper)
                 or (strategy != "whisper" and c.rest >= REST_SECONDS)]
    for candidate in sorted(preferred, key=lambda c: (-int(c.whisper), -c.rest, c.mora)):
        for i, (left, right) in enumerate(pairwise(edges)):
            if left.mora < candidate.mora < right.mora and fits(candidate, left, right):
                edges.insert(i + 1, candidate)
                break
    if strategy == "hybrid":
        i = 0
        while i < len(edges) - 1:
            left, right = edges[i:i + 2]
            choices = [c for c in candidates if left.mora < c.mora < right.mora
                       and fits(c, left, right)]
            if choices and (right.mora - left.mora > MAX_MORAS
                            or right.left_end - left.right_start > MAX_SECONDS):
                # Prefer about 24 moras / 7 seconds, with nearby rests as a tie-break.
                chosen = min(choices, key=lambda c: (
                    ((c.mora - left.mora) / 24 - 1) ** 2
                    + ((c.left_end - left.right_start) / 7 - 1) ** 2
                    - min(c.rest, 1) * .2,
                    c.mora,
                ))
                edges.insert(i + 1, chosen)
            else:
                i += 1
    return edges[1:-1]


def prepare_lyric_phrases(
    project: Project, *, enabled: bool,
    strategy: Literal["hybrid", "whisper", "rest"] = "hybrid",
) -> None:
    """Rebuild conversion lines while preserving canonical lyrics and note identity.

    Source lines are saved so converting again, including with the option off,
    starts from the authored grouping. Recognition/rest-only modes support offline
    comparison; the public boolean option uses the combined policy.
    """
    if strategy not in {"hybrid", "whisper", "rest"}:
        raise ValueError(f"未対応の歌詞分割方式: {strategy}")
    source_lines = ([Line(**row) for row in project.lyric_phrasing["source_lines"]]
                    if project.lyric_phrasing else copy.deepcopy(project.lines))
    if not enabled:
        if project.lyric_phrasing:
            project.lines = source_lines
            for line in source_lines:
                for nid in line.note_ids:
                    project.notes[nid].line = line.id
            project.lyric_phrasing = None
        return
    layers = project.lyric_layers
    overlay = next((e["detail"] for e in (layers or {}).get("evidence", [])
                    if e.get("kind") == "lyric-surface"
                    and e.get("detail", {}).get("mode") == "supplied-lyrics-first"), None)
    if layers is None or overlay is None:
        raise ValueError("歌詞の自動分割には入力歌詞付きの音源解析結果が必要です")
    if len(source_lines) != len(layers["canonical"]):
        raise ValueError("入力歌詞と変換行の対応が失われています")
    lines: list[Line] = []
    decisions = []
    for index, (line, canonical) in enumerate(zip(source_lines, layers["canonical"], strict=True)):
        moras = split_fine_moras(canonical["kana"])
        start, end = project.line_time_range(line)
        candidates = _line_boundaries(project, line, canonical, overlay["recognized_lines"])
        cuts = _choose_boundaries(candidates, start, end, len(moras), len(canonical["text"]),
                                  strategy)
        if not cuts:
            lines.append(replace(line, id=len(lines), canonical_line_index=index))
            continue
        boundaries = [(0, 0)] + [(c.text, c.mora) for c in cuts]
        boundaries.append((len(canonical["text"]), len(moras)))
        for (ta, ma), (tb, mb) in pairwise(boundaries):
            ids = canonical["mora_ids"][ma:mb]
            owned = set(ids)
            note_ids = [nid for nid in line.note_ids
                        if owned.intersection(layers["synthesis_plan"][nid]["mora_ids"])]
            text = canonical["text"][ta:tb]
            notes = [project.notes[nid] for nid in note_ids]
            lines.append(replace(
                line, id=len(lines), xf_surface=text, original_text=text,
                # Distinct groups prevent original-granularity captions rejoining the phrases.
                original_line_index=len(lines), note_ids=note_ids,
                xf_kana="".join(note.kana for note in notes), canonical_kana="".join(moras[ma:mb]),
                canonical_start_sec=min(note.start_sec for note in notes),
                canonical_end_sec=max(note.end_sec for note in notes),
                canonical_line_index=index, canonical_mora_ids=list(ids),
            ))
        decisions.append({"source_line_id": line.id, "boundaries": [asdict(c) for c in cuts]})
    # Untouched lines also need distinct display IDs after earlier lines expanded.
    for line in lines:
        line.original_line_index = line.id
    project.lines = lines
    for line in lines:
        for nid in line.note_ids:
            project.notes[nid].line = line.id
    project.lyric_phrasing = {
        "strategy": strategy, "source_lines": [asdict(line) for line in source_lines],
        "decisions": decisions,
    }
