"""Conservatively recognize dictionary-backed reading annotations in lyrics."""

from __future__ import annotations

import csv
import re
import unicodedata
from functools import lru_cache

import jaconv

from .kana import normalize_long_vowels

_KANJI = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af々〆〇"
_SURFACE = re.compile(rf"[{_KANJI}][{_KANJI}ぁ-ゖァ-ヶー]*$")
_START = re.compile(rf"(?<![{_KANJI}])[{_KANJI}]")
_KANA = re.compile(r"[ァ-ヶー]+")
_PAIRS = {"(": ")", "（": "）", "[": "]", "［": "］",
          "{": "}", "【": "】", "「": "」", "『": "』", "《": "》"}
_READING_OPENERS = frozenset("(（[［")
_TOKENS = re.compile(
    r"[|｜][^|｜《\r\n]+《[^《》\r\n]+》|\\.|[()（）\[\]［］{}【】「」『』《》]"
)


def _katakana(text: str) -> str:
    return jaconv.hira2kata(unicodedata.normalize("NFKC", text))


@lru_cache(maxsize=512)
def _dictionary_readings(surface: str, annotation_end: int | None = None) -> tuple[str, ...]:
    """Use complete known-word analyses; never drop an unknown character.

    Each call owns its Tagger, so concurrent lyric jobs cannot mix N-best state.
    This deliberately avoids the ruby-aware reading functions and their recursion.
    """
    import MeCab
    import unidic_lite

    tagger = MeCab.Tagger("-d " + unidic_lite.DICDIR)
    if annotation_end is None:
        annotation_end = len(surface)
    node = tagger.parseToNode(surface)
    cursor = 0
    while node:
        if node.surface:
            fields = next(csv.reader([node.feature]))
            if fields[0] == "助詞" and cursor < annotation_end:
                return ()
            cursor += len(node.surface)
        node = node.next
    if not tagger.parseNBestInit(surface):
        return ()
    readings: list[str] = []
    for _ in range(8):
        node = tagger.nextNode()
        if node is None:
            break
        parts = []
        surfaces = []
        cursor = 0
        while node:
            if node.surface:
                fields = next(csv.reader([node.feature]))
                pronunciation = fields[9] if len(fields) > 9 else ""
                if node.stat == MeCab.MECAB_UNK_NODE or not _KANA.fullmatch(pronunciation):
                    break
                # A repeated phrase such as 君の夢(きみのゆめ) can be an echo.
                # Particles may follow okurigana, but cannot be inside the base.
                if fields[0] == "助詞" and cursor < annotation_end:
                    break
                parts.append(pronunciation)
                surfaces.append(node.surface)
                cursor += len(node.surface)
            node = node.next
        if "".join(surfaces) == surface:
            reading = normalize_long_vowels("".join(parts))
            if reading not in readings:
                readings.append(reading)
    return tuple(readings)


def _matches(surface: str, reading: str, following: str) -> bool:
    if normalize_long_vowels(reading) in _dictionary_readings(surface):
        return True
    # 生(い)きる: validate the inflected word, including its okurigana.
    # Stop before a following particle whose pronunciation differs (は -> ワ).
    return any(
        normalize_long_vowels(reading + _katakana(following[:end]))
        in _dictionary_readings(surface + following[:end], len(surface))
        for end in range(len(following), 0, -1)
    )


def normalize_parenthetical_ruby(text: str) -> str:
    """Convert adjacent kanji/kana readings to explicit ruby, keeping other text.

    Only matching (), （）, [] and ［］ with kana-only contents qualify. Whitespace,
    nesting, unknown readings and kana-only bases remain literal. Existing explicit
    ruby and escaped punctuation are left alone. Dictionary failure keeps the input.
    """
    if not any(char in text for char in _READING_OPENERS):
        return text
    stack: list[tuple[str, int]] = []
    edits: list[tuple[int, int, str]] = []
    for token in _TOKENS.finditer(text):
        char = token.group()
        if len(char) != 1:  # Explicit ruby or an escaped character.
            continue
        if char in _PAIRS:
            stack.append((char, token.start()))
            continue
        if not stack or _PAIRS[stack[-1][0]] != char:
            continue
        opener, start = stack.pop()
        if stack or opener not in _READING_OPENERS:
            continue
        reading = _katakana(text[start + 1:token.start()])
        if not _KANA.fullmatch(reading):
            continue
        base = _SURFACE.search(text, 0, start)
        if base is None:
            continue
        tail = re.match(r"[ぁ-ゖ]+", text[token.end():])
        following = tail[0] if tail else ""
        for candidate in reversed(list(_START.finditer(base[0]))):
            offset = base.start() + candidate.start()
            surface = text[offset:start]
            try:
                matched = _matches(surface, reading, following)
            except (ImportError, RuntimeError):
                return text
            if matched:
                edits.append((offset, token.end(), f"｜{surface}《{reading}》"))
                break
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text
