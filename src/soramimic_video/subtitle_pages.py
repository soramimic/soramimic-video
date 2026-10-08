"""Fit display-only lyric pages to their box without changing the song timeline."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import cache

from .align import SubtitleSegment, _default_reader, _pron_normalize
from .project import Line, ParodyWord, Project


@dataclass
class SubtitlePage:
    text: str
    start: float
    end: float
    words: list[ParodyWord] = field(default_factory=list)


@dataclass
class _Atom:
    text: str
    start: float
    end: float
    words: list[ParodyWord] = field(default_factory=list)


_FALLBACK_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|[^\s]")
_PARTICLES = {"は", "が", "を", "に", "へ", "と", "で", "の", "も", "や", "から", "まで", "より"}
_CLOSING = set("、。，．！？!?：:；;)]）］｝」』】〉》ーぁぃぅぇぉっゃゅょァィゥェォッャュョ")
_OPENING = set("([（［｛「『【〈《")


def _text_tokens(text: str) -> list[tuple[str, str]]:
    """Retain every surface character, including spaces skipped by the reader."""
    tokens = _default_reader(text)
    located: list[tuple[int, str]] = []
    cursor = 0
    for surface, reading in tokens:
        if not surface:
            continue
        index = text.find(surface, cursor)
        if index < 0 or text[cursor:index].strip():
            located = []
            break
        if not surface.isspace():
            located.append((index, reading))
        cursor = index + len(surface)
    if not located or text[cursor:].strip():
        located = [(m.start(), m.group()) for m in _FALLBACK_TOKEN.finditer(text)]
    if not located:
        return [(text, "")]
    located[0] = (0, located[0][1])
    bounds = [start for start, _ in located] + [len(text)]
    return [(text[bounds[i]:bounds[i + 1]], reading)
            for i, (_, reading) in enumerate(located)]


def _reading_positions(source: str, target: str) -> list[int]:
    """Map text-reading boundaries to existing note-reading boundaries monotonically."""
    positions = [0] * (len(source) + 1)
    if not source:
        return positions
    matcher = SequenceMatcher(None, source, target, autojunk=False)
    opcodes = matcher.get_opcodes()
    # Unknown readings still use note boundaries, never equal divisions of time.
    if sum(block.size for block in matcher.get_matching_blocks()) < len(source) * .35:
        opcodes = [("replace", 0, len(source), 0, len(target))]
    for _tag, a, b, c, d in opcodes:
        if a == b:
            positions[a] = d
            continue
        for index in range(a, b + 1):
            positions[index] = c + round((index - a) * (d - c) / (b - a))
    return positions


def _original_atoms(
    project: Project, segment: SubtitleSegment, lines: Sequence[Line],
) -> list[_Atom]:
    notes = [project.notes[n] for k in segment.indices for n in lines[k].note_ids]
    note_readings = [_pron_normalize(n.kana) for n in notes]
    target = _pron_normalize("".join(note_readings))
    owners = [note for note, reading in zip(notes, note_readings, strict=True) for _ in reading]
    if not owners:
        return [_Atom(segment.text, segment.start, segment.end)]
    tokens = _text_tokens(segment.text)
    readings = [_pron_normalize(reading) for _, reading in tokens]
    source = _pron_normalize("".join(readings))
    positions = _reading_positions(source, target)
    atoms = []
    cursor = 0
    for (text, _), reading in zip(tokens, readings, strict=True):
        a = positions[cursor]
        cursor += len(reading)
        b = positions[cursor]
        start = owners[min(a, len(owners) - 1)].start_sec
        end = owners[min(b - 1, len(owners) - 1)].end_sec if b > a else start
        atoms.append(_Atom(text, start, end))
    return atoms


def _parody_atoms(project: Project, words: list[ParodyWord], sep: str) -> list[_Atom]:
    atoms = []
    for i, word in enumerate(words):
        notes = [project.notes[n] for n in word.note_ids]
        start = min((n.start_sec for n in notes), default=0.0)
        end = max((n.end_sec for n in notes), default=start)
        atoms.append(_Atom(word.surface + (sep if i + 1 < len(words) else ""), start, end, [word]))
    return atoms


def _break_quality(left: str, right: str) -> float | None:
    """Prefer authored lines, punctuation, spaces, and phrase-ending particles."""
    before, after = left.rstrip(), right.lstrip()
    if not before or not after or before[-1] in _OPENING or after[0] in _CLOSING:
        return None
    if re.search(r"[A-Za-z0-9]$", left) and re.match(r"[A-Za-z0-9]", right):
        return None
    if after in _PARTICLES:
        return None
    if "\n" in left or before[-1] in "。！？.!?":
        return 0.0
    if before[-1] in "、，,;；" or before in _PARTICLES:
        return .1
    if left[-1].isspace():
        return .15
    return .4


def _visible_spans(
    start: float, end: float, clear_ranges: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Subtract dedicated screens without discarding the later singing interval."""
    spans = []
    cursor = start
    for clear_start, clear_end in sorted(clear_ranges):
        if clear_end <= cursor or clear_start >= end or clear_end <= clear_start:
            continue
        if cursor < clear_start:
            spans.append((cursor, clear_start))
        cursor = min(end, clear_end)
    if cursor < end:
        spans.append((cursor, end))
    return spans


def paginate_subtitle(
    project: Project,
    segment: SubtitleSegment,
    source: str,
    lines: Sequence[Line],
    words: list[ParodyWord],
    measure: Callable[[str], float],
    max_width: float,
    sep: str = "  ",
    lead_sec: float = .15,
    clear_ranges: Sequence[tuple[float, float]] = (),
) -> list[SubtitlePage]:
    """Split at dedicated screens, then fit each singing interval to readable pages.

    Shared or overlapping notes remain on one page. An indivisible oversized word
    stays whole and is fitted by the renderer. Only display text/times are returned;
    the project, synthesis, and caller's lyric grouping are never modified.
    """
    spans = _visible_spans(segment.start, segment.end, clear_ranges)
    if not spans:
        return []
    whole = SubtitlePage(segment.text, spans[0][0], spans[-1][1], words)
    if len(spans) == 1 and measure(segment.text) <= max_width:
        return [whole]
    atoms = (_parody_atoms(project, words, sep) if source == "parody" and words
             else _original_atoms(project, segment, lines))
    if len(spans) == 1:
        return _paginate_atoms(whole, atoms, source, measure, max_width, lead_sec)

    groups: list[list[_Atom]] = [[] for _ in spans]
    for atom in atoms:
        owners = [i for i, (start, end) in enumerate(spans)
                  if atom.start < end and start < atom.end]
        # Untimed punctuation and unmatched readings stay with the nearest singing
        # interval. A single word sung on both sides is shown again after the break.
        if not owners:
            owners = [min(range(len(spans)), key=lambda i: max(
                spans[i][0] - atom.start, atom.start - spans[i][1], 0.0,
            ))]
        for i in owners:
            groups[i].append(atom)
    pages = []
    for (start, end), group in zip(spans, groups, strict=True):
        if not group:
            continue
        part = SubtitlePage("".join(a.text for a in group).strip(), start, end,
                            [word for atom in group for word in atom.words])
        pages.extend(_paginate_atoms(part, group, source, measure, max_width, lead_sec))
    return pages


def _paginate_atoms(
    whole: SubtitlePage, atoms: list[_Atom], source: str,
    measure: Callable[[str], float], max_width: float, lead_sec: float,
) -> list[SubtitlePage]:
    """Fit one uninterrupted display interval; all cut times stay inside it."""
    if measure(whole.text) <= max_width:
        return [whole]
    if len(atoms) < 2:
        return [whole]
    cuts = [0]
    times = [whole.start]
    penalties = [0.0]
    latest_end = atoms[0].end
    for i in range(1, len(atoms)):
        previous, current = atoms[i - 1], atoms[i]
        quality = .15 if source == "parody" else _break_quality(previous.text, current.text)
        when = current.start - lead_sec
        if (quality is not None and current.start >= latest_end - 1e-6
                and when >= times[-1] + .2 and when <= whole.end - .2):
            cuts.append(i)
            times.append(when)
            penalties.append(quality)
        latest_end = max(latest_end, current.end)
    cuts.append(len(atoms))
    times.append(whole.end)
    penalties.append(0.0)
    if len(cuts) == 2:
        return [whole]

    @cache
    def page_text(a: int, b: int) -> str:
        return "".join(atom.text for atom in atoms[cuts[a]:cuts[b]]).strip()

    # Few pages, but prefer natural boundaries and avoid a tiny final page or flashes.
    costs = [float("inf")] * len(cuts)
    following = [len(cuts) - 1] * len(cuts)
    costs[-1] = 0.0
    for a in range(len(cuts) - 2, -1, -1):
        for b in range(a + 1, len(cuts)):
            ratio = measure(page_text(a, b)) / max(1, max_width)
            if ratio > 1 and b > a + 1:
                break
            duration = times[b] - times[a]
            cost = (1 + .8 * (1 - min(ratio, 1)) ** 2 + penalties[b]
                    + 3 * max(0, 1 - duration / .8) ** 2
                    + 5 * max(0, ratio - 1) + costs[b])
            if cost < costs[a]:
                costs[a] = cost
                following[a] = b
    pages = []
    a = 0
    while a < len(cuts) - 1:
        b = following[a]
        page_words = [word for atom in atoms[cuts[a]:cuts[b]] for word in atom.words]
        pages.append(SubtitlePage(page_text(a, b), times[a], times[b], page_words))
        a = b
    return pages
