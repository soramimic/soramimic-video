"""Keep lyric word/phrase boundaries while retaining the confirmed reading."""

from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher
from typing import Any

from .align import _pron_normalize
from .kana import split_fine_moras


def project_reading(
    tokens: list[dict[str, Any]], kana: str,
) -> list[dict[str, Any]] | None:
    """Project whole-token boundaries, never interpolate inside a reading edit.

    Tokenization provides surface forms, parts of speech and phrase indices;
    ``kana`` remains authoritative for pronunciation. Ambiguous boundaries or
    boundaries inside a mora cannot be used to divide the confirmed reading.
    """
    readings = [re.sub(r"[^ァ-ヴー]", "", t.get("pronunciation", "")) for t in tokens]
    for i, token in enumerate(tokens):
        if token.get("pos") == "助詞":
            readings[i] = {"は": "ワ", "へ": "エ", "を": "オ"}.get(
                token["surface_form"], readings[i],
            )
    source = _pron_normalize("".join(readings).replace("ヲ", "オ"))
    target = _pron_normalize(kana.replace("ヲ", "オ"))
    if not source or not target:
        return None
    matcher = SequenceMatcher(None, source, target, autojunk=False)
    if matcher.ratio() < .6:
        return None
    offsets: dict[int, set[int]] = {}
    for tag, a, b, c, d in matcher.get_opcodes():
        for left, right in ((a, c), (b, d)):
            offsets.setdefault(left, set()).add(right)
        if tag == "equal":
            for i in range(b - a + 1):
                offsets.setdefault(a + i, set()).add(c + i)
    if offsets.get(0) != {0} or offsets.get(len(source)) != {len(target)}:
        return None

    # These normalizations preserve character positions. Only original mora
    # boundaries are eligible; e.g. キ / ャ must never become separate tokens.
    mora_ends = {0}
    cursor = 0
    for mora in split_fine_moras(kana):
        cursor += len(mora)
        mora_ends.add(cursor)
    result = copy.deepcopy(tokens)
    source_cursor = target_cursor = 0
    for token, reading in zip(result, readings, strict=True):
        if not reading:
            continue  # punctuation/whitespace stays attached to its surface
        source_cursor += len(reading)
        candidates = offsets.get(source_cursor, set())
        if len(candidates) != 1:
            return None
        end = next(iter(candidates))
        if end not in mora_ends or end <= target_cursor:
            return None
        token["pronunciation"] = kana[target_cursor:end]
        token["reading"] = token["pronunciation"]
        target_cursor = end
    if target_cursor != len(kana):
        return None
    return result
