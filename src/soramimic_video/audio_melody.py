"""Audio melody transcription with SheetSage2.

SheetSage2 is an optional, local-only primary note source.  Its non-commercial
weights are never downloaded by the application: operators must explicitly
configure already-reviewed model directories. Official lyrics are aligned by
``mora_align`` separately; this module only supplies note candidates.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from . import runproc

logger = logging.getLogger(__name__)

SHEETSAGE_MODEL_ENV = "SORAMIMIC_SHEETSAGE_MODEL_DIR"
SHEETSAGE_BASE_ENV = "SORAMIMIC_SHEETSAGE_BASE_DIR"
_MODEL_CACHE: dict[tuple[str, ...], Any] = {}


@dataclass(frozen=True)
class MelodyNote:
    start_sec: float
    end_sec: float
    midi_note: int


def _configured_local_capabilities() -> dict[str, bool]:
    """Return local model capability flags without importing or downloading models."""
    def configured_path(name: str) -> Path | None:
        value = os.environ.get(name, "").strip()
        return Path(value).expanduser() if value else None

    model = configured_path(SHEETSAGE_MODEL_ENV)
    base = configured_path(SHEETSAGE_BASE_ENV)
    return {
        "sheetsage2": bool(
            model is not None
            and base is not None
            and (model / "config.json").is_file()
            and (model / "model.safetensors").is_file()
            and (model / "LICENSE").is_file()
            and (base / "config.json").is_file()
            and (base / "model.safetensors").is_file()
            and (base / "LICENSE").is_file()
        )
    }


def configured_capabilities() -> dict[str, bool]:
    """Return local and shared-service model capabilities."""
    from .audio_inference import configured_url

    capabilities = _configured_local_capabilities()
    if configured_url() is not None:
        capabilities["sheetsage2"] = True
    return capabilities


def read_sheetsage_notes(path: Path) -> list[MelodyNote]:
    """Read SheetSage2's vocal LAB output and normalize its song-clock geometry."""
    notes: list[MelodyNote] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        fields = raw.split("\t")
        if len(fields) != 3:
            raise ValueError(f"SheetSage2のノート出力が不正です({number}行目)")
        start, end, pitch = float(fields[0]), float(fields[1]), int(fields[2])
        if not np.isfinite((start, end)).all() or start < 0 or end <= start:
            raise ValueError(f"SheetSage2のノート時刻が不正です({number}行目)")
        if not 0 <= pitch <= 127:
            raise ValueError(f"SheetSage2の音高が不正です({number}行目)")
        notes.append(MelodyNote(start, end, pitch))
    notes.sort(key=lambda note: (note.start_sec, note.end_sec, note.midi_note))
    normalized: list[MelodyNote] = []
    clipped = 0
    for note in notes:
        if normalized and normalized[-1].end_sec > note.start_sec + 1e-6:
            previous = normalized[-1]
            if note.start_sec <= previous.start_sec + 1e-6:
                raise ValueError("SheetSage2の同時刻ボーカルノートが競合しています")
            # Windowed inference can repeat a short part of a pitch transition.
            # Preserve both onsets and pitches, and assign the shared time to the
            # later note just as the downstream monophonic renderer does.
            normalized[-1] = replace(previous, end_sec=note.start_sec)
            clipped += 1
        normalized.append(note)
    if clipped:
        logger.warning("SheetSage2の重複ノート境界を%d件整理しました", clipped)
    return normalized


def transcribe_sheetsage(
    audio_path: Path,
    output_dir: Path,
    *,
    device: str,
    on_progress: Callable[[float], None] | None = None,
) -> list[MelodyNote] | None:
    """Run SheetSage2 through the shared service when configured."""
    from .audio_inference import configured_url, transcribe_sheetsage_remote

    if configured_url() is not None:
        logger.info("共有SheetSage2サービスでボーカルノートを抽出中")
        return transcribe_sheetsage_remote(
            audio_path,
            device,
            on_progress=on_progress,
        )
    return _transcribe_sheetsage_local(
        audio_path,
        output_dir,
        device=device,
        on_progress=on_progress,
    )


def _transcribe_sheetsage_local(
    audio_path: Path,
    output_dir: Path,
    *,
    device: str,
    on_progress: Callable[[float], None] | None = None,
) -> list[MelodyNote] | None:
    """Run a configured local SheetSage2 model, or return None when unconfigured."""
    capabilities = _configured_local_capabilities()
    if not capabilities["sheetsage2"]:
        return None
    try:
        import soundfile as sf
        import torch
        from transformers import AutoModel
    except ImportError as exc:
        raise RuntimeError(
            "SheetSage2用の依存がありません(uv sync --extra audio)"
        ) from exc

    model_dir = Path(os.environ[SHEETSAGE_MODEL_ENV]).resolve()
    base_dir = Path(os.environ[SHEETSAGE_BASE_ENV]).resolve()
    key = ("sheetsage2", str(model_dir), str(base_dir), device)
    model = _MODEL_CACHE.get(key)
    if model is None:
        logger.info("ローカルSheetSage2モデルを読み込んでいます")
        model = AutoModel.from_pretrained(
            str(model_dir),
            base_model_path=str(base_dir),
            trust_remote_code=True,
            local_files_only=True,
            torch_dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
        ).eval().to(device)
        _MODEL_CACHE[key] = model

    output_dir.mkdir(parents=True, exist_ok=False)
    samples, rate = sf.read(audio_path, dtype="float32", always_2d=True)
    if not np.isfinite(samples).all():
        raise ValueError("音源に非有限値が含まれています")

    def progress(event: dict[str, Any]) -> None:
        runproc.raise_if_cancelled()
        if on_progress is None:
            return
        windows = max(1, int(event.get("windows", 1)))
        window = min(windows, max(0, int(event.get("window", 0))))
        on_progress(window / windows)

    logger.info("SheetSage2でボーカルノートを抽出中")
    with torch.inference_mode():
        result = model.transcribe(
            samples.mean(axis=1),
            sampling_rate=rate,
            output_dir=str(output_dir),
            melody_only=True,
            progress=progress,
        )
    runproc.raise_if_cancelled()
    warnings = result.get("warnings", []) if isinstance(result, dict) else []
    if warnings:
        logger.warning("SheetSage2の診断: %s", "; ".join(map(str, warnings)))
    lab = output_dir / "melody_vocal.lab"
    if not lab.is_file():
        raise RuntimeError("SheetSage2のボーカルノート出力がありません")
    if on_progress:
        on_progress(1.0)
    return read_sheetsage_notes(lab)
