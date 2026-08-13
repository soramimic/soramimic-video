"""Validate externally deployed XF MIDI sample catalogs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SampleValidation:
    sample_id: str
    notes: int
    lines: int
    matched_lines: int


def _manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"{path} はサンプル配列である必要があります")
    return data


def validate_sample_directory(
    directory: Path, *, local_only: bool = False
) -> list[SampleValidation]:
    """Parse every catalog entry and verify its MIDI/lyrics alignment."""

    from .align import align_lines
    from .xfparse import analyze_midi

    directory = directory.resolve()
    manifests = [directory / "samples.local.json"]
    if not local_only:
        manifests.insert(0, directory / "samples.json")
    entries = [entry for path in manifests for entry in _manifest(path)]
    if not entries:
        names = "samples.local.json" if local_only else "samples.json / samples.local.json"
        raise ValueError(f"{directory} に検査対象の {names} がありません")

    seen: set[str] = set()
    results: list[SampleValidation] = []
    for entry in entries:
        sample_id = entry.get("id")
        if not isinstance(sample_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", sample_id
        ):
            raise ValueError(f"不正なサンプルID: {sample_id!r}")
        if sample_id in seen:
            raise ValueError(f"サンプルIDが重複しています: {sample_id}")
        seen.add(sample_id)

        midi_path = directory / f"{sample_id}.mid"
        lyrics_path = directory / f"{sample_id}_lyrics.txt"
        missing = [str(path) for path in (midi_path, lyrics_path) if not path.is_file()]
        if missing:
            raise ValueError(f"{sample_id}: ファイルがありません: {', '.join(missing)}")

        lyric_lines = [
            line.strip()
            for line in lyrics_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not lyric_lines:
            raise ValueError(f"{sample_id}: 元歌詞が空です")

        project = analyze_midi(midi_path)
        align_lines(project, lyric_lines)
        empty_kana = sum(not note.kana for note in project.notes)
        matched = sum(line.original_text is not None for line in project.lines)
        if empty_kana:
            raise ValueError(f"{sample_id}: 読みが空の音符が {empty_kana} 個あります")
        if matched != len(project.lines):
            raise ValueError(
                f"{sample_id}: 元歌詞に対応しないXF行があります "
                f"({matched}/{len(project.lines)}行が対応)"
            )
        results.append(SampleValidation(sample_id, len(project.notes), len(project.lines), matched))
    return results
