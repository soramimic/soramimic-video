"""Optional whole-line adjustment of supplied lyrics using audio recognition."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from soramimic_score import LyricLine, adjust_known_lyrics

from .audio_melody import MelodyNote
from .reading import text_to_kana
from .semantic_lyrics import coalesce_repeated_suffix_fragments, decide_recognized_lines
from .transcribe import TranscribedLine


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
