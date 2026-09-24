"""Optional whole-line adjustment of supplied lyrics using audio recognition."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from soramimic_score import LyricLine, SurfaceLine, adjust_known_lyrics, align_lyric_surface

from .audio_melody import MelodyNote
from .project import Project
from .reading import text_to_kana
from .ruby import strip_ruby
from .semantic_lyrics import coalesce_repeated_suffix_fragments, decide_recognized_lines
from .transcribe import TranscribedLine


def apply_supplied_surface(
    project: Project, supplied: list[str], *,
    refine: Callable[[int, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Match display spelling without replacing the automatic canonical graph.

    Several recognized lines may share one display group. Source indices are
    occurrence identifiers, so repeated choruses remain separate subtitles.
    Unmatched supplied text is retained, not classified as absent from the audio.
    """
    if project.lyric_layers is None:
        raise ValueError("音源解析の歌詞レイヤーが必要です")
    overlay = align_lyric_surface(
        [SurfaceLine(line.xf_surface, line.canonical_kana or line.xf_kana,
                     text_to_kana(line.xf_surface)) for line in project.lines],
        [SurfaceLine(strip_ruby(text), text_to_kana(text)) for text in supplied],
    )
    overlay["supplied_lines"] = supplied.copy()  # Preserve explicit ruby as input evidence.
    reviews = []
    for group in overlay["groups"]:
        indices = group["asr_indices"]
        group["line_ids"] = [project.lines[i].id for i in indices]
        group["start_sec"] = project.line_time_range(project.lines[indices[0]])[0]
        group["end_sec"] = project.line_time_range(project.lines[indices[-1]])[1]
        if group["operation"] == "match" and refine is not None:
            if len(indices) == 1 and len(group["supplied_indices"]) == 1:
                review = refine(indices[0], supplied[group["supplied_indices"][0]])
                reviews.append({"asr_indices": indices, **review})
                group["original_acoustic_reading"] = group["acoustic_reading"]
                group["acoustic_reading"] = project.lines[indices[0]].canonical_kana
            else:
                reviews.append({"asr_indices": indices, "status": "unresolved-group"})
    # Refinement may rebuild lines; apply the surface layer only after it finishes.
    for group_index, group in enumerate(overlay["groups"]):
        for index in group["asr_indices"]:
            line = project.lines[index]
            line.original_text = group["display_text"]
            line.original_line_index = group_index
    overlay["reading_reviews"] = reviews
    overlay["acoustic_changes"] = any(row["status"] == "applied" for row in reviews)
    project.lyric_layers["lyric_surface"] = overlay
    return overlay


def adjust_supplied_lines(
    supplied: list[str],
    recognized: list[TranscribedLine],
    melody: list[MelodyNote],
    *,
    vocals: Path | None,
) -> tuple[list[str], dict[str, Any]]:
    """Filter unsupported recognition before delegating whole-line matching."""
    lines, _merges = coalesce_repeated_suffix_fragments(recognized)
    decisions = decide_recognized_lines(lines, melody)
    supported = [True] * len(lines)
    if vocals is not None:
        from .vocal_activity import measure_vocal_activity

        profile = measure_vocal_activity(
            vocals, [(line.start_sec, line.end_sec) for line in lines],
        )
        supported = [item.supported for item in profile.lines]
    retained = [
        LyricLine(line.text, line.start_sec, line.end_sec)
        for line, decision, active in zip(lines, decisions, supported, strict=True)
        if decision.status != "rejected" and active
    ]
    try:
        result = adjust_known_lyrics(supplied, retained, reading=text_to_kana)
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(
            "入力歌詞と音源の対応を確認できず、歌詞の削除・補完を停止しました。"
            "歌詞と音源を確認するか、削除・補完をオフにしてください。"
        ) from exc
    return [line.text for line in result.lines], result.detail
