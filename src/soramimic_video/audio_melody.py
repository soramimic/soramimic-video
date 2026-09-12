"""Audio melody transcription and conservative gap recovery.

SheetSage2 is an optional, local-only primary note source.  Its non-commercial
weights are never downloaded by the application: operators must explicitly
configure already-reviewed model directories.  Official lyrics are aligned by
``mora_align`` separately; this module only supplies pitch provenance.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import runproc
from .mora_align import AlignedMora

logger = logging.getLogger(__name__)

SHEETSAGE_MODEL_ENV = "SORAMIMIC_SHEETSAGE_MODEL_DIR"
SHEETSAGE_BASE_ENV = "SORAMIMIC_SHEETSAGE_BASE_DIR"
RMVPE_ROOT_ENV = "SORAMIMIC_RMVPE_ROOT"
RMVPE_CHECKPOINT_ENV = "SORAMIMIC_RMVPE_CHECKPOINT"
FCPE_CHECKPOINT_ENV = "SORAMIMIC_FCPE_CHECKPOINT"

_SAMPLE_RATE = 16000
_HOP = 160
_MODEL_CACHE: dict[tuple[str, ...], Any] = {}


@dataclass(frozen=True)
class MelodyNote:
    start_sec: float
    end_sec: float
    midi_note: int


@dataclass(frozen=True)
class PitchEvidence:
    times: np.ndarray
    midi: np.ndarray
    confidence: np.ndarray
    family: str


@dataclass(frozen=True)
class MoraPitch:
    midi_note: int
    source: str
    confidence: float | None = None


def configured_capabilities() -> dict[str, bool]:
    """Return model capability flags without importing or downloading models."""
    def configured_path(name: str) -> Path | None:
        value = os.environ.get(name, "").strip()
        return Path(value).expanduser() if value else None

    model = configured_path(SHEETSAGE_MODEL_ENV)
    base = configured_path(SHEETSAGE_BASE_ENV)
    rmvpe_root = configured_path(RMVPE_ROOT_ENV)
    rmvpe_checkpoint = configured_path(RMVPE_CHECKPOINT_ENV)
    fcpe_checkpoint = configured_path(FCPE_CHECKPOINT_ENV)
    fcpe_spec = importlib.util.find_spec("torchfcpe")
    if fcpe_checkpoint is None and fcpe_spec is not None and fcpe_spec.origin:
        fcpe_checkpoint = Path(fcpe_spec.origin).parent / "assets/fcpe_c_v001.pt"
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
        ),
        "rmvpe": bool(
            rmvpe_root is not None
            and rmvpe_checkpoint is not None
            and (rmvpe_root / "modules/pe/rmvpe/inference.py").is_file()
            and rmvpe_checkpoint.is_file()
        ),
        "fcpe": bool(fcpe_checkpoint is not None and fcpe_checkpoint.is_file()),
    }


def read_sheetsage_notes(path: Path) -> list[MelodyNote]:
    """Read SheetSage2's vocal LAB output and validate its song-clock geometry."""
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
    if any(
        a.end_sec > b.start_sec + 1e-6
        for a, b in zip(notes, notes[1:], strict=False)
    ):
        raise ValueError("SheetSage2のボーカルノートが重複しています")
    return notes


def transcribe_sheetsage(
    audio_path: Path,
    output_dir: Path,
    *,
    device: str,
    on_progress: Callable[[float], None] | None = None,
) -> list[MelodyNote] | None:
    """Run a configured local SheetSage2 model, or return None when unconfigured."""
    capabilities = configured_capabilities()
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


def _waveform(path: Path) -> np.ndarray:
    import librosa

    values, _ = librosa.load(str(path), sr=_SAMPLE_RATE, mono=True)
    if not np.isfinite(values).all():
        raise ValueError("音源に非有限値が含まれています")
    return np.asarray(values, dtype=np.float32)


def _evidence(f0: np.ndarray, confidence: np.ndarray, family: str) -> PitchEvidence:
    import librosa

    f0 = np.asarray(f0, dtype=float).reshape(-1)
    confidence = np.asarray(confidence, dtype=float).reshape(-1)
    size = min(len(f0), len(confidence))
    f0, confidence = f0[:size], confidence[:size]
    midi = librosa.hz_to_midi(np.where(f0 > 0, f0, np.nan))
    times = np.arange(size, dtype=float) * _HOP / _SAMPLE_RATE
    confidence[~np.isfinite(confidence)] = 0
    return PitchEvidence(times, midi, np.clip(confidence, 0, 1), family)


def _chunked(
    waveform: np.ndarray,
    predict: Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]],
    *,
    chunk_seconds: float = 30.0,
    context_seconds: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Join centered 100 Hz predictions while keeping GPU memory bounded."""
    frame_count = len(waveform) // _HOP + 1
    core_frames = max(1, round(chunk_seconds * _SAMPLE_RATE / _HOP))
    context_frames = max(0, round(context_seconds * _SAMPLE_RATE / _HOP))
    f0 = np.zeros(frame_count, dtype=float)
    confidence = np.zeros(frame_count, dtype=float)
    for first in range(0, frame_count, core_frames):
        runproc.raise_if_cancelled()
        last = min(frame_count, first + core_frames)
        start = max(0, first - context_frames)
        stop = min(len(waveform), (last + context_frames) * _HOP)
        part = waveform[start * _HOP : stop]
        padded = np.pad(part, (0, max(0, 2048 - len(part))))
        pitches, scores = predict(padded)
        pitches = np.asarray(pitches).reshape(-1)
        scores = np.asarray(scores).reshape(-1)
        left, right = first - start, last - start
        if len(pitches) < right or len(scores) != len(pitches):
            raise RuntimeError("音高モデルが互換性のない時刻列を返しました")
        f0[first:last] = pitches[left:right]
        confidence[first:last] = scores[left:right]
    return f0, confidence


def extract_rmvpe(path: Path, *, device: str) -> PitchEvidence | None:
    caps = configured_capabilities()
    if not caps["rmvpe"]:
        return None
    import torch

    root = Path(os.environ[RMVPE_ROOT_ENV]).resolve()
    checkpoint = Path(os.environ[RMVPE_CHECKPOINT_ENV]).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import modules.pe.rmvpe.inference as implementation
    from modules.pe.rmvpe import RMVPE

    if not Path(implementation.__file__).resolve().is_relative_to(root):
        raise RuntimeError("別のRMVPE実装が既に読み込まれています")
    key = ("rmvpe", str(root), str(checkpoint), device)
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = RMVPE(str(checkpoint), device=torch.device(device))
        _MODEL_CACHE[key] = model
    def predict(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        with torch.inference_mode():
            wave = torch.from_numpy(values).float().unsqueeze(0).to(device)
            hidden = model.mel2hidden(model.mel_extractor(wave, center=True))
            f0 = np.asarray(
                model.decode(hidden, thred=0.03, use_viterbi=False)
            ).reshape(-1)
            confidence = hidden.amax(dim=-1).squeeze(0).cpu().numpy()
        return f0, confidence

    f0, confidence = _chunked(_waveform(path), predict)
    f0[(f0 < 65) | (f0 > 1100)] = 0
    return _evidence(f0, confidence, "rmvpe")


def extract_fcpe(path: Path, *, device: str) -> PitchEvidence | None:
    if not configured_capabilities()["fcpe"]:
        return None
    import torch
    import torchfcpe

    configured = os.environ.get(FCPE_CHECKPOINT_ENV, "").strip()
    checkpoint = (
        Path(configured).resolve()
        if configured
        else Path(torchfcpe.__file__).parent / "assets/fcpe_c_v001.pt"
    )
    if not checkpoint.is_file():
        return None
    key = ("fcpe", str(checkpoint), device)
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = torchfcpe.spawn_infer_model_from_pt(str(checkpoint), device=device)
        _MODEL_CACHE[key] = model
    def predict(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        with torch.inference_mode():
            wave = torch.from_numpy(values).float()[None, :, None].to(device)
            latent = model.model(model.wav2mel(wave, _SAMPLE_RATE))
            cents = model.model.latent2cents_local_decoder(latent, threshold=0.006)
            f0 = model.model.cent_to_f0(cents).squeeze().cpu().numpy().reshape(-1)
            confidence = latent.amax(dim=-1).squeeze(0).cpu().numpy()
        return f0, confidence

    f0, confidence = _chunked(_waveform(path), predict)
    f0[(f0 < 65) | (f0 > 1100)] = 0
    return _evidence(f0, confidence, "fcpe")


def _mode(track: PitchEvidence | None, start: float, end: float) -> tuple[int, float] | None:
    if track is None or end <= start:
        return None
    selected = (track.times >= start) & (track.times < end)
    values = track.midi[selected]
    confidence = track.confidence[selected]
    good = np.isfinite(values) & (confidence >= 0.5)
    if len(values) < 3 or good.mean() < 0.65:
        return None
    keys = np.rint(values[good]).astype(int)
    unique, counts = np.unique(keys, return_counts=True)
    index = int(np.argmax(counts))
    if counts[index] / len(keys) < 0.65:
        return None
    return int(unique[index]), float(good.mean())


def _sheetsage_pitch(mora: AlignedMora, notes: list[MelodyNote]) -> int | None:
    overlaps = [
        (max(0.0, min(mora.end_sec, note.end_sec) - max(mora.start_sec, note.start_sec)), note)
        for note in notes
        if note.start_sec < mora.end_sec and note.end_sec > mora.start_sec
    ]
    if not overlaps:
        return None
    overlap, note = max(overlaps, key=lambda item: (item[0], item[1].end_sec - item[1].start_sec))
    required = min(0.04, max(0.01, (mora.end_sec - mora.start_sec) * 0.25))
    return note.midi_note if overlap >= required else None


def assign_mora_pitches(
    moras: list[AlignedMora],
    sheetsage_notes: list[MelodyNote],
    *,
    rmvpe: PitchEvidence | None,
    fcpe: PitchEvidence | None,
    fallback_midi: list[int],
) -> list[MoraPitch]:
    """Keep SheetSage notes and fill only its lyric-bearing gaps conservatively."""
    if len(moras) != len(fallback_midi):
        raise ValueError("モーラとフォールバック音高の数が一致しません")
    result: list[MoraPitch] = []
    for mora, fallback in zip(moras, fallback_midi, strict=True):
        model_pitch = _sheetsage_pitch(mora, sheetsage_notes)
        if model_pitch is not None:
            result.append(MoraPitch(model_pitch, "sheetsage_note", 1.0))
            continue
        primary = _mode(rmvpe, mora.start_sec, mora.end_sec)
        confirmation = _mode(fcpe, mora.start_sec, mora.end_sec)
        if primary and confirmation and abs(primary[0] - confirmation[0]) <= 1:
            result.append(
                MoraPitch(primary[0], "recovered_note", min(primary[1], confirmation[1]))
            )
        else:
            # Existing consumers need a MIDI value for synthesis.  The value is only
            # a rendering fallback; ``source=spoken`` preserves that pitch is unknown.
            result.append(MoraPitch(int(fallback), "spoken", None))
    return result
