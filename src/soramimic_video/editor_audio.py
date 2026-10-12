"""Align the lyrics and readings already chosen in an editor export."""

from __future__ import annotations

from typing import Any


def editor_audio_lines(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Keep source phrase order, including empty rows, without selecting readings."""
    from soramimic_score import normalize_lyric_input, normalize_reading

    units = payload["unitsList"]
    candidates = [payload.get("originalLines"), payload.get("phrases")]
    if isinstance(payload.get("lyrics"), str):
        candidates.append(payload["lyrics"].splitlines())
    texts = next((rows for rows in candidates if isinstance(rows, list)
                  and len(rows) == len(units) and all(isinstance(s, str) for s in rows)), None)
    if texts is None:
        raise ValueError("歌詞JSONに、読みと行数が一致する元歌詞が必要です")
    result = []
    for index, (text, row) in enumerate(zip(texts, units, strict=True)):
        normalize_lyric_input(text)
        if not row and not text.strip():
            if payload["results"][index]:
                raise ValueError(f"歌詞JSONの{index + 1}行目: 空行に単語が割り当てられています")
            result.append((text, ""))
            continue
        if (not text.strip() or not row or any(not isinstance(unit, dict)
                or not isinstance(unit.get("pronunciation"), str)
                or not unit["pronunciation"] for unit in row)):
            raise ValueError(f"歌詞JSONの{index + 1}行目に元歌詞と読みが必要です")
        try:
            reading = normalize_reading("".join(unit["pronunciation"] for unit in row))
        except ValueError as exc:
            raise ValueError(f"歌詞JSONの{index + 1}行目の読みが不正です: {exc}") from exc
        result.append((text, reading))
    if not any(reading for _, reading in result):
        raise ValueError("歌詞JSONに読みのある歌詞が必要です")
    return result
