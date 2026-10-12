"""Align an edited song's source lyrics with analyzed lyrics across line breaks."""

from __future__ import annotations

import copy
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from itertools import accumulate, pairwise
from typing import Any

from .align import _map_correct_offset_to_recognized, _pron_normalize
from .convert import _engine_kana, engine_phrases
from .parenthetical_ruby import normalize_parenthetical_ruby
from .project import Line, Project
from .ruby import strip_ruby
from .soramimic_engine import conversion_tokens


def editor_analysis_lyrics(payload: dict[str, Any]) -> str | None:
    """Supply the editor's original reading to XF/Whisper lyric alignment.

    The selected parody reading is never an analysis input. Ruby annotations
    carry the already-known original reading through the normal supplied-lyrics
    pipeline, including localization against Whisper and forced alignment.
    """
    from soramimic_score import validate_reading

    readings = ["".join(unit["pronunciation"] for unit in row)
                for row in payload.get("unitsList", [])]
    if not any(readings):
        return None
    phrases = payload.get("phrases") or []
    tokens = payload.get("tokensList") or []
    lines = []
    for index, reading in enumerate(readings):
        if not reading:
            continue  # Empty editor rows are not sung lyrics.
        reading = validate_reading(reading).kana
        surface = (phrases[index] if len(phrases) == len(readings)
                   and isinstance(phrases[index], str) else reading)
        if len(tokens) == len(readings) and tokens[index]:
            # Video exports kana phrases with original spelling in tokensList.
            surface = "".join(token["surface_form"] for token in tokens[index]) or surface
        surface = strip_ruby(normalize_parenthetical_ruby(surface))
        lines.append(f"｜{surface}《{reading}》")
    return "\n".join(lines)


def _mora_ids(project: Project, line: Line) -> list[str]:
    if line.canonical_mora_ids is not None:
        return line.canonical_mora_ids
    layers = project.lyric_layers
    assert layers is not None
    if line.note_ids:
        owner = layers["synthesis_plan"][line.note_ids[0]]["utterance_id"]
        return next(row["mora_ids"] for row in layers["canonical"]
                    if row["utterance_id"] == owner)
    index = line.canonical_line_index
    return layers["canonical"][line.id if index is None else index]["mora_ids"]


def _merge_lines(project: Project, members: list[Line], line_id: int) -> Line:
    if len(members) == 1:
        return replace(members[0], id=line_id)
    texts = []
    previous: Line | None = None
    for line in members:
        # An XF lyric can span several display lines. Keep that supplied text
        # once, but retain identical lyrics belonging to distinct occurrences.
        if (previous is None or line.original_line_index is None
                or line.original_line_index != previous.original_line_index
                or line.original_text != previous.original_text):
            texts.append(line.original_text or line.xf_surface or line.xf_kana)
        previous = line
    notes = [nid for line in members for nid in line.note_ids]
    layered = project.lyric_layers is not None
    return Line(
        id=line_id, xf_surface="".join(line.xf_surface for line in members),
        xf_kana="".join(line.xf_kana for line in members), note_ids=notes,
        original_text=" ".join(texts), original_line_index=line_id,
        canonical_kana=("".join(line.canonical_kana or "" for line in members)
                        if layered else None),
        canonical_mora_ids=([mid for line in members for mid in _mora_ids(project, line)]
                            if layered else None),
        canonical_start_sec=(min(project.line_time_range(line)[0] for line in members)
                             if layered else None),
        canonical_end_sec=(max(project.line_time_range(line)[1] for line in members)
                           if layered else None),
    )


def align_editor_lines(project: Project, payload: dict[str, Any]) -> dict[str, Any]:
    """Rebase edited word spans onto analyzed lyrics, without reconverting words.

    Source units describe the lyrics that were replaced, not the selected parody
    reading. Compare that entire lyric with the analyzed lyric before distributing
    results to its lines. Notes, canonical lyrics and their ID graph stay intact.
    A word crossing an analyzed line break stays whole by joining those lines.
    """
    source_lines = [
        {"units": units, "words": words}
        for units, words in zip(payload["unitsList"], payload["results"], strict=True)
    ]
    target_phrases = engine_phrases(project)
    source_phrases = ["".join(unit["pronunciation"] for unit in row["units"])
                      for row in source_lines]
    if source_phrases == target_phrases:
        return {"lines": source_lines, "phrases": payload.get("phrases", source_phrases),
                "tokensList": payload.get("tokensList", [])}

    units: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for row in source_lines:
        offset = len(units)
        for word in row["words"]:
            start, end = word["period"]
            if not 0 <= start < end <= len(row["units"]):
                raise ValueError("編集済み歌詞の単語範囲が不正です")
            words.append({**word, "period": [start + offset, end + offset]})
        units.extend(copy.deepcopy(row["units"]))
    source = "".join(source_phrases)
    target = "".join(target_phrases)
    if not source or not target:
        raise ValueError("編集済み歌詞と解析歌詞の読みが必要です")
    # The same pronunciation comparison and offset alignment used for supplied
    # lyrics absorbs long-vowel spelling and recognition differences. Comparing
    # full lyrics also distinguishes repeated occurrences across different breaks.
    source_reading = _pron_normalize(_engine_kana(source).replace("ヲ", "オ"))
    target_reading = _pron_normalize(target.replace("ヲ", "オ"))
    if len(source_reading) != len(source) or len(target_reading) != len(target):
        raise ValueError("編集済み歌詞の読みの文字位置を対応付けられません")
    matcher = SequenceMatcher(None, source_reading, target_reading, autojunk=False)
    if matcher.ratio() < 0.6:
        raise ValueError("編集済みデータの元歌詞と曲の歌詞を対応付けられません")
    opcodes = matcher.get_opcodes()
    source_offsets = list(accumulate((len(u["pronunciation"]) for u in units), initial=0))
    offsets = [_map_correct_offset_to_recognized(opcodes, pos, len(target))
               for pos in source_offsets]
    offsets[0], offsets[-1] = 0, len(target)

    # Retain edited word readings/locks/IDs; only their source-unit coordinates
    # change. Units with no analyzed counterpart carry no duration.
    mapped_units = []
    unit_indices = [0]
    for unit, (start, end) in zip(units, pairwise(offsets), strict=True):
        if end > start:
            mapped_units.append({**unit, "pronunciation": target[start:end]})
        unit_indices.append(len(mapped_units))
    for word in words:
        start, end = word["period"]
        word["period"] = [unit_indices[start], unit_indices[end]]
        if unit_indices[start] == unit_indices[end]:
            raise ValueError(f"編集済みの単語「{word['surface']}」に対応する歌詞がありません")

    unit_offsets = list(accumulate((len(u["pronunciation"]) for u in mapped_units), initial=0))
    word_spans = [(unit_offsets[w["period"][0]], unit_offsets[w["period"][1]])
                  for w in words]
    target_offsets = list(accumulate(map(len, target_phrases), initial=0))
    groups = [0]
    for index, boundary in enumerate(target_offsets[1:-1], start=1):
        if (boundary in unit_offsets
                and not any(start < boundary < end for start, end in word_spans)):
            groups.append(index)
    groups.append(len(project.lines))
    aligned_lines: list[Line] = []
    converted = []
    for first, last in pairwise(groups):
        start, end = target_offsets[first], target_offsets[last]
        ua, ub = unit_offsets.index(start), unit_offsets.index(end)
        row_words = [{**word, "period": [word["period"][0] - ua, word["period"][1] - ua]}
                     for word in words if ua <= word["period"][0] < ub]
        converted.append({"units": mapped_units[ua:ub], "words": row_words})
        aligned_lines.append(_merge_lines(project, project.lines[first:last], len(aligned_lines)))

    if len(aligned_lines) != len(project.lines):
        # Reuse the reversible grouping record, so a later conversion can start
        # from the original analyzed lines instead of these editing groups.
        if project.lyric_phrasing is None:
            project.lyric_phrasing = {
                "strategy": "editor-alignment",
                "source_lines": [asdict(line) for line in project.lines],
                "decisions": [], "diagnostics": [],
            }
        for line in aligned_lines:
            line.original_line_index = line.id
    project.lines = aligned_lines
    for line in project.lines:
        for nid in line.note_ids:
            project.notes[nid].line = line.id
    phrases = engine_phrases(project)
    return {"lines": converted, "phrases": phrases,
            "tokensList": conversion_tokens(
                phrases, [line.original_text or line.xf_surface for line in project.lines],
                payload.get("param"),
            )}
