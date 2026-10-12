"""Generate only the gaps in an imported editor result."""

from __future__ import annotations

from itertools import accumulate
from typing import Any

from .convert import (
    _engine_kana,
    project_conversion_tokens,
    project_note_length_weights,
    project_word_boundaries,
    resolve_convert_settings,
    resolve_wordlist,
)
from .project import Project
from .soramimic_engine import run_convert


def complete_editor_gaps(
    project: Project, raw: dict[str, Any], wordlist: str, where: str | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Keep every existing word, whether locked or not, and fill uncovered units."""
    saved = []
    missing = False
    for row in raw["lines"]:
        words = sorted((word for word in row["words"] if not word.get("filler")),
                       key=lambda word: word["period"][0])
        cursor = 0
        for word in words:
            start, end = word["period"]
            if not cursor <= start < end <= len(row["units"]):
                raise ValueError("編集済み歌詞の単語範囲が不正です")
            missing |= start > cursor
            cursor = end
        missing |= cursor < len(row["units"])
        saved.append(words)
    if not missing:
        return raw

    csv_path = resolve_wordlist(wordlist)
    where, coerced, alpha = resolve_convert_settings(csv_path, where, params)
    phrases = [_engine_kana("".join(unit["pronunciation"] for unit in row["units"]))
               for row in raw["lines"]]

    def locks(units_per_line: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
        result = []
        for row, words, units, phrase in zip(
            raw["lines"], saved, units_per_line, phrases, strict=True,
        ):
            before = list(accumulate((len(u["pronunciation"]) for u in row["units"]), initial=0))
            after = list(accumulate((len(u["pronunciation"]) for u in units), initial=0))
            if "".join(u["pronunciation"] for u in units) != phrase:
                raise ValueError("未作成部分の生成で元歌詞の読みが変わりました")
            indices = {offset: index for index, offset in enumerate(after)}
            mapped = []
            for word in words:
                start, end = word["period"]
                if before[start] not in indices or before[end] not in indices:
                    raise ValueError("編集済み単語の境界を保って未作成部分を生成できません")
                mapped.append({**word, "period": [indices[before[start]], indices[before[end]]]})
            result.append(mapped)
        return result

    return run_convert(
        phrases, csv_path, where, coerced,
        weights_per_line=project_note_length_weights(project, alpha) if alpha > 0 else None,
        cache_db=False,
        word_boundaries_per_line=project_word_boundaries(project),
        tokens_list=project_conversion_tokens(project, coerced),
        locks_per_line=locks,
    )
