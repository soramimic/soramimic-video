"""Adapt Soramimic Score's complete audio analysis to a Video project."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path

from .audio_project import DEFAULT_BPM
from .project import Project, SongInfo
from .score_layers import (
    generation_quality_assessment,
    lyric_order_conflict_unit_ids,
    omit_unresolved_synthesis_units,
)

ANALYZE_DIR = "analyze_audio"


def _torch_device(device: str | None) -> str:
    """Resolve local torch inference placement without loading an audio model."""
    if device is not None:
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


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
    editor_path: Path | None = None,
) -> Project:
    """Run the Score pipeline and import only its supported synthesis slots."""
    from soramimic_score import ModelConfig, parse_lyric_input
    from soramimic_score import analyze_audio as analyze_score

    from . import runproc
    from .audio_inference import configured_priority, configured_url
    from .lyric_layers import apply_lyric_layers

    if adjust_lyrics and editor_path is not None:
        raise ValueError("歌詞JSONの歌詞と読みは調整できません")
    if adjust_lyrics and lyrics_path is None:
        raise ValueError("歌詞の調整には入力歌詞が必要です")
    if bpm <= 0:
        raise ValueError("BPMは正の値が必要です")
    lyrics = None
    parsed = None
    input_receipt = None
    editor_rows = None
    if editor_path is not None:
        from .editor_audio import editor_audio_lines
        from .editor_io import validate_editor_upload

        payload = validate_editor_upload(editor_path.read_bytes())
        editor_rows = editor_audio_lines(payload)
        lyrics = tuple(text for text, _ in editor_rows)
        input_receipt = {"source": "editor-json", "lines": [
            {"text": text, "reading": kana} for text, kana in editor_rows],
        }
    elif lyrics_path is not None:
        # Decode without universal-newline translation so the audit retains the
        # exact supplied spelling, whitespace, blank lines, and line endings.
        original_lyrics = lyrics_path.read_bytes().decode("utf-8")
        parsed = parse_lyric_input(original_lyrics)
        lyrics = parsed.lyric_lines
        input_receipt = parsed.to_dict()
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
    out = project_dir / ANALYZE_DIR
    out.mkdir(parents=True, exist_ok=True)
    input_path = out / "lyric_input.json"
    if input_receipt is not None:
        input_path.write_text(json.dumps(input_receipt, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    else:
        input_path.unlink(missing_ok=True)
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

    if editor_rows is not None:
        from .editor_audio import align_editor_audio

        document = align_editor_audio(audio_path, editor_rows, config,
                                      on_progress, accompaniment)
    else:
        document = analyze_score(
            audio_path, lyrics=parsed,
            model_config=config, adjust_lyrics=adjust_lyrics,
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
    order_conflicts = (lyric_order_conflict_unit_ids(layers)
                       & set(layers.get("unresolved_unit_ids", [])))
    omitted = omit_unresolved_synthesis_units(layers)
    tempo = round(60_000_000 / bpm)
    project = Project(SongInfo(
        midi_path="", ticks_per_beat=480, tempo_map=[[0, tempo]],
        audio_path=str(audio_path),
        accompaniment_path=str(accompaniment) if accompaniment is not None else None,
    ))
    apply_lyric_layers(project, layers)
    quality = generation_quality_assessment(layers)
    pitch_unresolved = omitted - len(order_conflicts)
    limitations = ([f"音高が未解決の{pitch_unresolved}歌唱単位を合成から省略しました。"]
                   if pitch_unresolved else [])
    diagnostics = ([{"stage": "score", "status": "synthesis-omission",
                     "unit_count": omitted}] if omitted else [])
    if order_conflicts:
        limitations.append(
            f"歌詞の順序と発音時刻が矛盾する{len(order_conflicts)}歌唱単位を合成から省略しました。"
        )
        diagnostics.append({"stage": "score", "status": "lyric-order-conflict",
                            "unit_count": len(order_conflicts)})
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
        "asr_used": editor_rows is None,
        "fixed_editor_readings": editor_rows is not None,
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
