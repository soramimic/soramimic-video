"""Adapt Soramimic Score's complete audio analysis to a Video project."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path

from .audio_project import DEFAULT_BPM
from .project import Project, SongInfo


def analyze_audio(
    audio_path: Path,
    project_dir: Path,
    lyrics_path: Path | None = None,
    bpm: float = DEFAULT_BPM,
    whisper_model: str = "large-v3",
    skip_separation: bool = False,
    device: str | None = None,
    progress: Callable[[float], None] | None = None,
    adjust_lyrics: bool = False,
    progress_detail: Callable[[str], None] | None = None,
) -> Project:
    """Run the Score pipeline and import only its supported synthesis slots."""
    from soramimic_score import ModelConfig
    from soramimic_score import analyze_audio as analyze_score

    from . import runproc
    from .analyze_audio import (
        _generation_quality_assessment,
        _omit_unresolved_synthesis_units,
        _torch_device,
    )
    from .audio_inference import configured_priority, configured_url
    from .lyric_layers import apply_lyric_layers

    if adjust_lyrics and lyrics_path is None:
        raise ValueError("歌詞の調整には入力歌詞が必要です")
    if bpm <= 0:
        raise ValueError("BPMは正の値が必要です")
    lyrics = None
    if lyrics_path is not None:
        lyrics = [line.strip() for line in lyrics_path.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        if not lyrics:
            raise ValueError("入力歌詞が空です")

    model = os.environ.get("SORAMIMIC_SHEETSAGE_MODEL_DIR", "").strip()
    base = os.environ.get("SORAMIMIC_SHEETSAGE_BASE_DIR", "").strip()
    if not model or not base:
        raise RuntimeError("音源解析にはSheetSage2のモデルディレクトリ設定が必要です")
    config = ModelConfig(
        sheetsage_model=Path(model).expanduser(),
        sheetsage_base=Path(base).expanduser(),
        whisper_model=whisper_model,
        device="cuda" if _torch_device(device) in {"cuda", "cuda:0"} else "cpu",
        local_files_only=True,
        separate_vocals=not skip_separation,
        shared_inference_url=configured_url(),
        shared_inference_priority=configured_priority(),
    )
    out = project_dir / "analyze_audio"
    out.mkdir(parents=True, exist_ok=True)
    annotation_path = out / "lyric_annotations.json"
    annotation_path.unlink(missing_ok=True)
    accompaniment = out / "no_vocals.wav" if not skip_separation else None
    if progress is not None:
        progress(0.01)

    progress_count = 0

    def on_progress(stage: str) -> None:
        nonlocal progress_count
        runproc.raise_if_cancelled()
        if progress_detail is not None:
            progress_detail(stage)
        if progress is not None:
            progress(min(0.9, progress_count * 0.04 + 0.02))
        progress_count += 1

    document = analyze_score(
        audio_path, lyrics=lyrics, model_config=config, adjust_lyrics=adjust_lyrics,
        on_progress=on_progress, accompaniment_path=accompaniment,
    )
    runproc.raise_if_cancelled()
    (out / "score.json").write_text(document.to_json(), encoding="utf-8")
    layers = copy.deepcopy(document.to_dict()["score"])
    selections = [item["detail"] for item in layers.get("evidence", [])
                  if item.get("kind") == "reading-selection"
                  and "parenthetical_readings" in item.get("detail", {})]
    if selections:
        annotation_path.write_text(json.dumps({
            "supplied_lines": lyrics, "resolved_groups": selections,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    if not layers["canonical"]:
        raise ValueError(
            "歌唱の根拠を確認できる歌詞が残りませんでした。音源と入力歌詞を確認してください。"
        )
    omitted = _omit_unresolved_synthesis_units(layers)
    tempo = round(60_000_000 / bpm)
    project = Project(SongInfo(
        midi_path="", ticks_per_beat=480, tempo_map=[[0, tempo]],
        audio_path=str(audio_path),
        accompaniment_path=str(accompaniment) if accompaniment is not None else None,
    ))
    apply_lyric_layers(project, layers)
    quality = _generation_quality_assessment(layers)
    limitations = ([f"音高が未解決の{omitted}歌唱単位を合成から省略しました。"]
                   if omitted else [])
    diagnostics = ([{"stage": "score", "status": "synthesis-omission",
                     "unit_count": omitted}] if omitted else [])
    alignment_warnings = [item for item in layers.get("evidence", [])
                          if item.get("kind") == "lyric-alignment-warning"
                          and item.get("detail", {}).get("status") == "retained"]
    if alignment_warnings:
        quality["status"] = "warning"
        quality["reason"] = quality["reason"] or "uncertain-lyric-alignment"
        quality["uncertain_lyric_lines"] = len(alignment_warnings)
        limitations.append(
            f"音符の裏付けを優先して{len(alignment_warnings)}行の歌唱を保持しました。"
            "歌詞や発音時刻が不確かなため、確認してください。"
        )
        diagnostics.append({
            "stage": "score", "status": "uncertain-lyric-alignment",
            "line_count": len(alignment_warnings),
            "evidence_ids": [item["id"] for item in alignment_warnings],
        })
    (out / "analysis.json").write_text(json.dumps({
        "audio_pipeline": "soramimic-score",
        "official_lyrics": lyrics is not None,
        "asr_used": True,
        "mora_count": sum(len(line["mora_ids"]) for line in layers["canonical"]),
        "sources": {source: sum(note.source == source for note in project.notes)
                    for source in {note.source for note in project.notes}},
        "generation_quality": quality,
        "limitations": limitations,
        "diagnostics": diagnostics,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    if progress is not None:
        progress(1.0)
    return project
