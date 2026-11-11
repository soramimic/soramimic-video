"""Portable display text / confirmed reading spans shared with the lyric editor.

The editor wire format uses UTF-16 half-open offsets. Unknown and stale optional
maps are ignored; they never replace either the authored text or its reading.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

from .align import _default_reader, _pron_normalize

FORMAT = "soramimic-lyric-alignment/1"
_FALLBACK_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|[^\s]")
TokenReader = Callable[[str], list[tuple[str, str]]]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def normalize_reading(text: str) -> str:
    return _pron_normalize(text).replace("を", "お")


def text_tokens(text: str, reader: TokenReader = _default_reader) -> list[tuple[str, str]]:
    """Keep all surface characters, including whitespace omitted by the reader."""
    tokens = reader(text)
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


def reading_positions(
    source: str, target: str, support: dict[str, bool] | None = None,
) -> list[int]:
    """Video's existing monotonic reading alignment, retaining its tie breaks."""
    positions = [0] * (len(source) + 1)
    if support is not None:
        support["aligned"] = bool(source) and source == target
    if not source:
        return positions
    if source == target:
        return list(range(len(source) + 1))
    matcher = SequenceMatcher(None, source, target, autojunk=False)
    opcodes = matcher.get_opcodes()
    supported = sum(block.size for block in matcher.get_matching_blocks()) >= len(source) * .35
    if support is not None:
        support["aligned"] = supported
    if not supported:
        opcodes = [("replace", 0, len(source), 0, len(target))]
    for _tag, a, b, c, d in opcodes:
        if a == b:
            positions[a] = d
            continue
        for index in range(a, b + 1):
            positions[index] = c + round((index - a) * (d - c) / (b - a))
    return positions


def align_tokens(text: str, reading: str, tokens: list[tuple[str, str]]) -> dict[str, Any] | None:
    if "".join(surface for surface, _ in tokens) != text:
        return None
    readings = [normalize_reading(part) for _, part in tokens]
    source, target = normalize_reading("".join(readings)), normalize_reading(reading)
    if (len(target) != len(reading) or utf16_length(reading) != len(reading)
            or len(source) * len(target) > 4_000_000):
        return None
    support: dict[str, bool] = {}
    positions = reading_positions(source, target, support)
    surface = offset = 0
    spans = []
    for (part, _), normalized in zip(tokens, readings, strict=True):
        a, first, c = surface, offset, positions[offset]
        surface += utf16_length(part)
        offset += len(normalized)
        d = positions[offset]
        exact = support["aligned"] and d > c and source[first:offset] == target[c:d]
        spans.append({"surface": [a, surface], "reading": [c, d], "exact": exact})
    return {"format": FORMAT, "text": text, "reading": reading, "spans": spans}


def valid_alignment(value: Any, text: str, reading: str) -> bool:
    if (not isinstance(text, str) or not isinstance(reading, str)
            or not isinstance(value, dict) or value.get("format") != FORMAT
            or value.get("text") != text or value.get("reading") != reading
            or not isinstance(value.get("spans"), list) or len(value["spans"]) > 10000):
        return False
    boundaries = {0}
    position = 0
    for char in text:
        position += utf16_length(char)
        boundaries.add(position)
    surface_end = reading_end = 0
    for span in value["spans"]:
        if (not isinstance(span, dict) or not isinstance(span.get("surface"), list)
                or len(span["surface"]) != 2 or not isinstance(span.get("reading"), list)
                or len(span["reading"]) != 2 or not isinstance(span.get("exact"), bool)):
            return False
        a, b = span["surface"]
        c, d = span["reading"]
        if (any(type(i) is not int for i in (a, b, c, d))
                or a < surface_end or b <= a or b > position
                or c < reading_end or d < c or d > len(reading)
                or a not in boundaries or b not in boundaries or (span["exact"] and c == d)):
            return False
        surface_end, reading_end = b, d
    return True


def build_alignment(text: str, reading: str, reader: TokenReader = _default_reader) -> dict | None:
    return align_tokens(text, reading, text_tokens(text, reader))


def sync_editor_alignments(payload: dict[str, Any]) -> None:
    """Upgrade old editor JSON lazily; preserve valid supplied correspondences."""
    previous = payload.get("originalReadingAlignments")
    originals = payload.get("originalLines") or []
    phrases = payload.get("phrases") or []
    result = []
    for index, units in enumerate(payload.get("unitsList") or []):
        text = (originals[index] if index < len(originals) and originals[index]
                else phrases[index] if index < len(phrases) else "")
        reading = "".join(unit.get("pronunciation", "") for unit in units)
        old = previous[index] if isinstance(previous, list) and index < len(previous) else None
        if valid_alignment(old, text, reading):
            result.append(old)
        else:
            if not text or not reading:
                result.append(None)
                continue
            tokens = payload.get("tokensList") or []
            row = tokens[index] if index < len(tokens) and isinstance(tokens[index], list) else []
            pairs = [(token.get("surface_form", ""), token.get("pronunciation", ""))
                     for token in row if isinstance(token, dict)
                     and isinstance(token.get("surface_form"), str)
                     and isinstance(token.get("pronunciation"), str)]
            known = (align_tokens(text, reading, pairs)
                     if "".join(part for part, _ in pairs) == text else None)
            result.append(known or (build_alignment(text, reading) if text and reading else None))
    payload["originalReadingAlignments"] = result
