"""Pinned KanaWhisper model adapter for the shared audio inference service."""

from __future__ import annotations

import gc
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

KANA_WHISPER_MODEL = "sbintuitions/kana-whisper"
KANA_WHISPER_REVISION = "88ecb3d79c5846cb4fcf76f4107b84c8fa2acd82"
KANA_CONTEXT_MAX_SEC = 24.0
KANA_MAX_WINDOWS = 256
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


def model_available() -> bool:
    """Return whether the pinned model snapshot is complete in the local cache."""
    try:
        from huggingface_hub import snapshot_download

        snapshot = Path(
            snapshot_download(
                KANA_WHISPER_MODEL,
                revision=KANA_WHISPER_REVISION,
                local_files_only=True,
            )
        )
    except Exception:  # noqa: BLE001 - health checks must degrade to unavailable
        return False
    return all(
        (snapshot / name).is_file()
        for name in (
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "tokenizer.json",
        )
    )


def transcribe_kana_windows(
    audio_path: Path,
    windows: Sequence[tuple[float, float]],
    device: str = "auto",
) -> list[str]:
    """Transcribe bounded windows through the shared service or locally."""
    from .audio_inference import configured_url, transcribe_kana_windows_remote

    if configured_url() is not None:
        return transcribe_kana_windows_remote(audio_path, windows, device)
    return _transcribe_kana_windows_local(audio_path, windows, device)


def _load_model(device: str) -> Any:
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    actual_device = "cuda" if device == "auto" and torch.cuda.is_available() else device
    if actual_device == "auto":
        actual_device = "cpu"
    dtype = torch.float16 if actual_device.startswith("cuda") else torch.float32
    key = (actual_device, str(dtype))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        KANA_WHISPER_MODEL,
        revision=KANA_WHISPER_REVISION,
        torch_dtype=dtype,
        use_safetensors=True,
    ).to(actual_device)
    processor = AutoProcessor.from_pretrained(
        KANA_WHISPER_MODEL,
        revision=KANA_WHISPER_REVISION,
    )
    transcriber = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=dtype,
        device=actual_device,
    )
    _MODEL_CACHE[key] = transcriber
    return transcriber


def _transcribe_kana_windows_local(
    audio_path: Path,
    windows: Sequence[tuple[float, float]],
    device: str = "auto",
    *,
    cancel_check: Callable[[], Any] | None = None,
) -> list[str]:
    """Run KanaWhisper for already validated song-clock windows."""
    try:
        import librosa
    except ImportError as exc:
        raise RuntimeError("KanaWhisper用の依存がありません(uv sync --extra audio)") from exc
    if len(windows) > KANA_MAX_WINDOWS:
        raise ValueError(f"KanaWhisper window count exceeds {KANA_MAX_WINDOWS}")
    waveform, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True)
    transcriber = _load_model(device)
    results: list[str] = []
    for start, end in windows:
        if cancel_check is not None:
            cancel_check()
        first = max(0, round(start * sample_rate))
        last = min(len(waveform), round(end * sample_rate))
        if first >= last:
            raise ValueError("KanaWhisper window is outside the input audio")
        output = transcriber(
            waveform[first:last],
            generate_kwargs={"language": "ja", "task": "transcribe"},
        )
        results.append(str(output["text"]).strip())
    return results


def release_kana_whisper_cache() -> None:
    """Release cached model memory for tests and controlled service maintenance."""
    _MODEL_CACHE.clear()
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass
