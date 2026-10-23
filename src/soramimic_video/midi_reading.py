"""Fill MIDI readings within lyric-event and lexical boundaries."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache

from .kana import normalize_long_vowels, split_fine_moras, vowel_of
from .project import Line, Note

NEEDS_READING = re.compile(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９\u3400-\u9fff々〆]")
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]")


def _moras(kana: str) -> tuple[str, ...]:
    return tuple(split_fine_moras(normalize_long_vowels(kana)))


def _equivalent(moras: tuple[str, ...], preceding: str | None = None) -> tuple[str, ...]:
    particles = {"ヲ": "オ", "ハ": "ワ", "ヘ": "エ"}
    result = []
    vowel = vowel_of(preceding) if preceding else None
    for mora in moras:
        value = vowel if mora == "ー" and vowel else particles.get(mora, mora)
        result.append(value)
        vowel = vowel_of(value)
    return tuple(result)


def _split_word(
    notes: list[Note], reading: str, inputs: dict[int, str], read: Callable[[str], str],
) -> tuple[str, ...] | None:
    """Accept one assignment inside a word, keeping supplied readings fixed.

    Missing runs use their own readings when those agree with the contextual
    word, a single event's full reading, or an exact one-mora-per-event match.
    Never divide a longer reading evenly over unrelated lyric events.
    """
    moras = _moras(reading)
    comparable = _equivalent(moras)

    @cache
    def assign(index: int, cursor: int) -> tuple[tuple[str, ...], ...]:
        if index == len(notes):
            return ((),) if cursor == len(moras) else ()
        note = notes[index]
        if note.kana:
            anchor = _equivalent(_moras(note.kana), comparable[cursor - 1] if cursor else None)
            if comparable[cursor:cursor + len(anchor)] != anchor:
                return ()
            return tuple((note.kana, *tail) for tail in assign(index + 1, cursor + len(anchor)))

        stop = index + 1
        while stop < len(notes) and not notes[stop].kana:
            stop += 1
        count = stop - index
        local = tuple(read(inputs.get(n.id, n.surface)) for n in notes[index:stop])
        ends = range(cursor + 1, len(moras) + 1) if stop < len(notes) else [len(moras)]
        solutions: list[tuple[str, ...]] = []
        for end in ends:
            segment = moras[cursor:end]
            if not segment:
                continue
            preceding = comparable[cursor - 1] if cursor else None
            if all(local) and (
                _equivalent(_moras("".join(local)), preceding)
                == _equivalent(segment, preceding)
            ):
                values = local
            elif count == 1:
                values = ("".join(segment),)
            elif len(segment) == count:
                values = segment
            else:
                continue
            for tail in assign(stop, end):
                candidate = (*values, *tail)
                if candidate not in solutions:
                    solutions.append(candidate)
                if len(solutions) == 2:
                    return tuple(solutions)
        return tuple(solutions)

    solutions = assign(0, 0)
    return solutions[0] if len(solutions) == 1 else None


def _unresolved(note: Note) -> ValueError:
    return ValueError(
        f"歌詞イベント「{note.surface or note.raw}」(tick={note.start_tick})の読みを"
        "音符に対応付けられません。MIDIの各歌詞イベントに「表記[かな]」で読みを指定してください"
    )


def fill_missing_kana(
    lines: list[Line], notes: list[Note], inputs: dict[int, str],
) -> int:
    """Infer only missing readings, preserving each event's text ownership."""
    from .reading import reading_tokens, text_to_kana

    @cache
    def read(text: str) -> str:
        return text_to_kana(text)

    filled = 0
    for line in lines:
        line_notes = [notes[i] for i in line.note_ids]
        missing = [n for n in line_notes if not n.kana]
        if not missing:
            continue

        # Latin lyrics can be whole words or syllables. Their MIDI events are
        # authoritative boundaries; concatenating them invents different words.
        for note in missing:
            text = inputs.get(note.id, note.surface)
            if _LATIN_OR_DIGIT.search(text) or text != note.surface:
                kana = read(text)
                if not kana:
                    raise _unresolved(note)
                note.kana = kana
                filled += 1

        if any(not n.kana for n in line_notes):
            tokens = reading_tokens(line.xf_surface)
            if "".join(surface for surface, _ in tokens) != line.xf_surface:
                raise _unresolved(next(n for n in line_notes if not n.kana))
            # Cut only where both a token and a lyric event end. A single event
            # may contain several tokens; one token may span several events.
            token_ends: set[int] = set()
            cursor = 0
            for surface, _ in tokens:
                cursor += len(surface)
                token_ends.add(cursor)
            cursor = token_index = token_cursor = start = 0
            for index, note in enumerate(line_notes):
                cursor += len(note.surface)
                if cursor not in token_ends or (
                    index + 1 < len(line_notes) and not line_notes[index + 1].surface
                ):
                    continue
                word_notes = line_notes[start:index + 1]
                reading_parts: list[str] = []
                while token_index < len(tokens) and token_cursor < cursor:
                    surface, kana = tokens[token_index]
                    reading_parts.append(kana)
                    token_cursor += len(surface)
                    token_index += 1
                if any(not n.kana for n in word_notes):
                    assigned = _split_word(word_notes, "".join(reading_parts), inputs, read)
                    if assigned is None:
                        raise _unresolved(next(n for n in word_notes if not n.kana))
                    for n, kana in zip(word_notes, assigned, strict=True):
                        if not n.kana:
                            n.kana = kana
                            filled += 1
                start = index + 1

        if any(not n.kana for n in line_notes):
            raise _unresolved(next(n for n in line_notes if not n.kana))
        line.xf_kana = "".join(n.kana for n in line_notes)
    return filled
