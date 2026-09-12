"""Import canonical/performed/synthesis layers without changing lyric meaning."""

from __future__ import annotations

import copy
import math
from typing import Any

from .project import Line, Note, Project


def apply_lyric_layers(project: Project, layers: dict[str, Any]) -> None:
    """Apply a version-1 realization atomically; unresolved units need editing.

    Import and playback do not require an inference model or wav_to_xf package.
    Acoustic confidence and synthesis interpolation remain distinct metadata.
    """
    if type(layers.get("schema_version")) is not int or layers["schema_version"] != 1:
        raise ValueError("未対応の歌詞レイヤーバージョンです")
    if project.parody is not None:
        raise ValueError("歌詞レイヤーの適用は替え歌変換より前に行ってください")
    if layers.get("unresolved_unit_ids"):
        raise ValueError("合成先が未解決の歌唱単位があります。歌詞を削らず対応を編集してください")
    from .timing_editor import sec_to_tick

    canonical = layers["canonical"]
    performed = layers["performed"]
    plan = layers["synthesis_plan"]
    by_unit = {item["singing_unit_id"]: item for item in performed}
    line_ids = {line["utterance_id"]: i for i, line in enumerate(canonical)}
    if len(line_ids) != len(canonical) or len(by_unit) != len(performed):
        raise ValueError("歌詞レイヤーに重複IDがあります")
    canonical_moras = [mora for line in canonical for mora in line["mora_ids"]]
    performed_moras = [mora for unit in performed for mora in unit["mora_ids"]]
    if (len(set(canonical_moras)) != len(canonical_moras)
            or sorted(canonical_moras) != sorted(performed_moras)):
        raise ValueError("完全歌詞の全モーラに歌唱単位の所属が必要です")
    omissions = {item["singing_unit_id"] for item in layers["omissions"]}
    evidence = {item["id"]: item for item in layers.get("evidence", [])}
    for item in layers["omissions"]:
        if (not item["reason"].strip() or not item["evidence_ids"]
                or any(eid not in evidence or evidence[eid]["kind"] != "performance-omission"
                       or evidence[eid]["confidence"] <= 0 for eid in item["evidence_ids"])):
            raise ValueError("実演の省略には明示的な根拠が必要です")
    rendered = {slot["singing_unit_id"] for slot in plan}
    if rendered & omissions or rendered | omissions != set(by_unit):
        raise ValueError("歌詞の各単位を合成または根拠付き省略として保持してください")
    notes: list[Note] = []
    note_ids: dict[int, list[int]] = {i: [] for i in range(len(canonical))}
    previous_end = 0.0
    previous_tick = 0
    for slot in plan:
        start, end, pitch = slot["start_sec"], slot["end_sec"], slot["midi_pitch"]
        if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start + end) or start < previous_end - 1e-9
                or end <= start or type(pitch) is not int or not 0 <= pitch <= 127
                or not slot["kana"] or slot["utterance_id"] not in line_ids):
            raise ValueError("合成ノートの時刻・音高・歌詞が不正です")
        unit = by_unit[slot["singing_unit_id"]]
        line_id = line_ids[slot["utterance_id"]]
        if (tuple(slot["mora_ids"]) != tuple(unit["mora_ids"])
                or not set(unit["mora_ids"]).issubset(canonical[line_id]["mora_ids"])):
            raise ValueError("合成ノートと歌唱単位のモーラが一致しません")
        start_tick = max(previous_tick, sec_to_tick(project.song, start))
        end_tick = max(start_tick + 1, sec_to_tick(project.song, end))
        note = Note(
            id=len(notes), midi_note=pitch, start_tick=start_tick, end_tick=end_tick,
            start_sec=start, end_sec=end, line=line_id, surface="", kana=slot["kana"],
            raw=slot["kana"], source="+".join(slot["pitch_sources"]),
            pitch_confidence=slot.get("pitch_confidence"),
        )
        notes.append(note)
        note_ids[line_id].append(note.id)
        previous_end, previous_tick = end, end_tick
    lines: list[Line] = []
    for i, line in enumerate(canonical):
        mora_ids = set(line["mora_ids"])
        units = [unit for unit in performed if mora_ids.intersection(unit["mora_ids"])]
        starts = [unit["start_sec"] for unit in units if unit["start_sec"] is not None]
        ends = [unit["end_sec"] for unit in units if unit["end_sec"] is not None]
        lines.append(Line(
            i, line["text"], "".join(notes[n].kana for n in note_ids[i]), note_ids[i],
            original_text=line["text"], canonical_kana=line["kana"],
            canonical_start_sec=min(starts, default=None),
            canonical_end_sec=max(ends, default=None),
        ))
    project.notes, project.lines = notes, lines
    project.lyric_layers = copy.deepcopy(layers)
