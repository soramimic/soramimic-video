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
class SubtitleTrack:
    segment: SubtitleSegment
    source: str
    words: list[ParodyWord]
    measure: Callable[[str], float]
    max_width: float


@dataclass
class _Atom:
    text: str
    start: float
    end: float
    words: list[ParodyWord] = field(default_factory=list)
    phrase_start: bool = True
    break_before: bool | None = None


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


def _phrase_boundaries(text: str) -> tuple[set[int], set[int]] | None:
    """Return bunsetsu starts and safe word starts in the original surface text.

    Use the same bunsetsu policy as conversion phrasing. Word boundaries are a
    fallback for oversized phrases; particles, auxiliaries, suffixes and words
    following a prefix still stay attached. An unavailable or unmappable parse
    leaves the existing token/punctuation safeguards in charge.
    """
    from soramimic_score import LyricMappingError, parse_lyric_input

    parsed = parse_lyric_input(text)
    if any(item.kind == "ruby" for item in parsed.annotations):
        return None
    try:
        from jphrase import PhraseSplitter

        parts = PhraseSplitter(
            consider_non_independent_nouns_and_verbs_as_breaks=False,
        ).split_text(parsed.text, output_type="detailed")
    except (ImportError, RuntimeError):
        return None
    if any(not token["surface_form"] for part in parts for token in part):
        return None
    parts = [[token for token in part if not token["surface_form"].isspace()]
             for part in parts]
    parts = [part for part in parts if part]
    tokens = [token for part in parts for token in part]
    if not tokens:
        return None
    try:
        spans = parsed.match_parts(tuple(token["surface_form"] for token in tokens))
    except LyricMappingError:
        return None
    phrase_starts = set()
    index = 0
    for part in parts:
        phrase_starts.add(spans[index][0])
        index += len(part)
    word_starts = set()
    for i, (token, (start, _end)) in enumerate(zip(tokens, spans, strict=True)):
        if (token.get("pos") not in {"助詞", "助動詞"}
                and token.get("pos_detail_1") != "接尾"
                and (i == 0 or tokens[i - 1].get("pos") != "接頭詞")):
            word_starts.add(start)
    return phrase_starts, word_starts


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
    boundaries = _phrase_boundaries(segment.text)
    atoms = []
    cursor = offset = 0
    for (text, _), reading in zip(tokens, readings, strict=True):
        a = positions[cursor]
        cursor += len(reading)
        b = positions[cursor]
        start = owners[min(a, len(owners) - 1)].start_sec
        end = owners[min(b - 1, len(owners) - 1)].end_sec if b > a else start
        atoms.append(_Atom(
            text, start, end,
            phrase_start=boundaries is None or offset in boundaries[0],
            break_before=None if boundaries is None else offset in boundaries[1],
        ))
        offset += len(text)
    return atoms


def _parody_atoms(project: Project, words: list[ParodyWord], sep: str) -> list[_Atom]:
    atoms = []
    for i, word in enumerate(words):
        notes = [project.notes[n] for n in word.note_ids]
        start = min((n.start_sec for n in notes), default=0.0)
        end = max((n.end_sec for n in notes), default=start)
        atoms.append(_Atom(word.surface + (sep if i + 1 < len(words) else ""), start, end, [word]))
    return atoms


def _break_quality(left: str, right: str, *, check_particle: bool = True) -> float | None:
    """Prefer authored lines, punctuation, spaces, and phrase-ending particles."""
    before, after = left.rstrip(), right.strip()
    if not before or not after or before[-1] in _OPENING or after[0] in _CLOSING:
        return None
    if re.search(r"[A-Za-z0-9]$", left) and re.match(r"[A-Za-z0-9]", right):
        return None
    if check_particle and after in _PARTICLES:
        return None
    if "\n" in left or before[-1] in "。！？.!?":
        return 0.0
    if before[-1] in "、，,;；" or before in _PARTICLES:
        return .1
    if left[-1].isspace():
        return .15
    return .4


def _atom_break_quality(left: _Atom, right: _Atom, source: str) -> float | None:
    if source == "parody":
        return .15
    if right.break_before is False:
        return None
    return _break_quality(left.text, right.text, check_particle=right.break_before is None)


def _page_indices(
    times: list[float], penalties: list[float], preferred: list[bool],
    measure_ratio: Callable[[int, int], float],
) -> list[tuple[int, int]]:
    """Try fitting bunsetsu pages first, then relax the fewest safe boundaries.

    Width is measured at the configured font size. Oversized indivisible units
    remain whole for the renderer to fit, even after the word-boundary fallback.
    """
    ratio = cache(measure_ratio)

    def choose(phrase_only: bool) -> list[tuple[int, int]] | None:
        allowed = [i for i, use in enumerate(preferred) if use or not phrase_only]
        costs = [(float("inf"), 0, 0.0)] * len(times)
        following = [len(times) - 1] * len(times)
        costs[-1] = (0.0, 0, 0.0)
        for a in reversed(allowed[:-1]):
            for b in allowed:
                if b <= a:
                    continue
                width = ratio(a, b)
                if width > 1 and (phrase_only or b > a + 1):
                    break
                duration = times[b] - times[a]
                if duration < .2 - 1e-6 and (a != 0 or b != len(times) - 1):
                    continue
                overflow, relaxed, cost = costs[b]
                candidate = (
                    overflow + max(0, width - 1),
                    relaxed + int(not preferred[b]),
                    cost + 1 + .8 * (1 - min(width, 1)) ** 2 + penalties[b]
                    + 3 * max(0, 1 - duration / .8) ** 2,
                )
                if candidate < costs[a]:
                    costs[a] = candidate
                    following[a] = b
        if costs[0][0] == float("inf"):
            return None
        pages = []
        a = 0
        while a < len(times) - 1:
            b = following[a]
            pages.append((a, b))
            a = b
        return pages

    return choose(True) or choose(False) or [(0, len(times) - 1)]


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


def paginate_subtitle_tracks(
    project: Project, tracks: Sequence[SubtitleTrack], lines: Sequence[Line],
    sep: str = "  ", lead_sec: float = .15,
    clear_ranges: Sequence[tuple[float, float]] = (),
) -> list[list[SubtitlePage]]:
    """Paginate matching lyric groups at boundaries safe for every displayed track.

    A wide replacement caption also splits its original counterpart (and vice
    versa). Words, ruby and original tokens stay whole, even when the only safe
    result requires the renderer to reduce the font size.
    """
    if len({track.source for track in tracks}) < 2:
        return [paginate_subtitle(
            project, t.segment, t.source, lines, t.words, t.measure, t.max_width,
            sep, lead_sec, clear_ranges,
        ) for t in tracks]
    segment = tracks[0].segment
    spans = _visible_spans(segment.start, segment.end, clear_ranges)
    result: list[list[SubtitlePage]] = [[] for _ in tracks]
    if not spans:
        return result
    if len(spans) == 1 and all(t.measure(t.segment.text) <= t.max_width for t in tracks):
        return [[SubtitlePage(t.segment.text, *spans[0], t.words)] for t in tracks]
    atoms = [(_parody_atoms(project, t.words, sep) if t.source == "parody" and t.words
              else _original_atoms(project, t.segment, lines)) for t in tracks]
    grouped: list[list[list[_Atom]]] = [[[] for _ in spans] for _ in tracks]
    for groups, track_atoms in zip(grouped, atoms, strict=True):
        for atom in track_atoms:
            owners = [i for i, (start, end) in enumerate(spans)
                      if atom.start < end and start < atom.end]
            if not owners:
                owners = [min(range(len(spans)), key=lambda i: max(
                    spans[i][0] - atom.start, atom.start - spans[i][1], 0.0,
                ))]
            for i in owners:
                groups[i].append(atom)
    for i, (start, end) in enumerate(spans):
        groups = [g[i] for g in grouped]
        cuts = [tuple(0 for _ in tracks)]
        times = [start]
        penalties = [0.0]
        preferred = [True]
        for when in sorted({a.start for group in groups for a in group}):
            if when - lead_sec < start + .2 or when - lead_sec > end - .2:
                continue
            indices = []
            qualities = []
            for track, group in zip(tracks, groups, strict=True):
                k = next((j for j, a in enumerate(group) if a.start >= when - 1e-6),
                         len(group))
                if k == 0 or k == len(group) or max(a.end for a in group[:k]) > when + 1e-6:
                    break
                quality = _atom_break_quality(group[k - 1], group[k], track.source)
                if quality is None:
                    break
                indices.append(k)
                qualities.append(quality)
            if len(indices) == len(tracks) and tuple(indices) != cuts[-1]:
                cuts.append(tuple(indices))
                times.append(when - lead_sec)
                penalties.append(sum(qualities) / len(qualities))
                preferred.append(all(group[k].phrase_start
                                     for group, k in zip(groups, indices, strict=True)))
        cuts.append(tuple(len(g) for g in groups))
        times.append(end)
        penalties.append(0.0)
        preferred.append(True)

        @cache
        def page_text(
            track: int, a: int, b: int, groups=groups, cuts=cuts,
        ) -> str:
            return "".join(atom.text for atom in groups[track][cuts[a][track]:cuts[b][track]]
                           ).strip()

        def measure_ratio(a: int, b: int) -> float:
            return max(t.measure(page_text(j, a, b)) / max(1, t.max_width)
                       for j, t in enumerate(tracks))

        for a, b in _page_indices(times, penalties, preferred, measure_ratio):
            for j, group in enumerate(groups):
                text = page_text(j, a, b)
                if text:
                    words = [w for atom in group[cuts[a][j]:cuts[b][j]] for w in atom.words]
                    result[j].append(SubtitlePage(text, times[a], times[b], words))
    return result


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
    preferred = [True]
    latest_end = atoms[0].end
    for i in range(1, len(atoms)):
        previous, current = atoms[i - 1], atoms[i]
        quality = _atom_break_quality(previous, current, source)
        when = current.start - lead_sec
        if (quality is not None and current.start >= latest_end - 1e-6
                and when >= whole.start + .2 and when <= whole.end - .2):
            cuts.append(i)
            times.append(when)
            penalties.append(quality)
            preferred.append(current.phrase_start)
        latest_end = max(latest_end, current.end)
    cuts.append(len(atoms))
    times.append(whole.end)
    penalties.append(0.0)
    preferred.append(True)
    if len(cuts) == 2:
        return [whole]

    @cache
    def page_text(a: int, b: int) -> str:
        return "".join(atom.text for atom in atoms[cuts[a]:cuts[b]]).strip()

    def measure_ratio(a: int, b: int) -> float:
        return measure(page_text(a, b)) / max(1, max_width)

    pages = []
    for a, b in _page_indices(times, penalties, preferred, measure_ratio):
        page_words = [word for atom in atoms[cuts[a]:cuts[b]] for word in atom.words]
        pages.append(SubtitlePage(page_text(a, b), times[a], times[b], page_words))
    return pages
