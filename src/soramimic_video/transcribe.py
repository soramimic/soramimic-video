"""Whisper による歌詞認識(元歌詞テキストが無いときのフォールバック)。

faster-whisper のセグメントをそのまま「行」として扱う。
認識誤りは替え歌変換の入力誤りとして伝播するため、
元歌詞がある場合は analyze-audio に --lyrics で渡すこと。
"""

from __future__ import annotations

import gc
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WHISPER_MODEL = "large-v3"
_MIN_WHISPER_CUDA_FREE_BYTES = 5800 * 1024**2


@dataclass
class TranscribedLine:
    start_sec: float
    end_sec: float
    text: str


def _cuda_free_bytes(device: str) -> int | None:
    """Return currently available CUDA memory without making CUDA mandatory."""
    if device != "auto" and not device.startswith("cuda"):
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        cuda_device = device if device.startswith("cuda") else "cuda"
        free_bytes, _ = torch.cuda.mem_get_info(cuda_device)
        return int(free_bytes)
    except (ImportError, RuntimeError, ValueError):
        return None


def _is_cuda_oom(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        detail = str(current).casefold()
        if "cuda" in detail and (
            "out of memory" in detail or "memory allocation" in detail
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _release_cuda_cache() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass


def _run_whisper(
    whisper_model: Any,
    vocals_path: Path,
    *,
    vad_filter: bool,
    condition_on_previous_text: bool,
) -> tuple[list[TranscribedLine], Any]:
    segments, info = whisper_model.transcribe(
        str(vocals_path),
        language="ja",
        vad_filter=vad_filter,
        condition_on_previous_text=condition_on_previous_text,
    )
    lines = [
        TranscribedLine(start_sec=s.start, end_sec=s.end, text=s.text.strip())
        for s in segments
        if s.text.strip()
    ]
    return lines, info


def transcribe_lines(
    vocals_path: Path,
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = "auto",
    *,
    vad_filter: bool = True,
    condition_on_previous_text: bool = True,
) -> list[TranscribedLine]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper がインストールされていません(uv sync --extra audio)"
        ) from e

    requested_device = device
    free_bytes = _cuda_free_bytes(requested_device)
    compute_type: str | None = None
    if free_bytes is not None and free_bytes < _MIN_WHISPER_CUDA_FREE_BYTES:
        logger.warning(
            "GPU空き容量が%.1fGiBのためWhisperをCPUで実行します",
            free_bytes / 1024**3,
        )
        device = "cpu"
        compute_type = "int8"

    logger.info("Whisper(%s, %s)で歌詞を認識中...", model_size, device)
    model_kwargs = {"device": device}
    if compute_type is not None:
        model_kwargs["compute_type"] = compute_type
    try:
        model = WhisperModel(model_size, **model_kwargs)
        lines, info = _run_whisper(
            model,
            vocals_path,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )
    except RuntimeError as exc:
        if device == "cpu" or not _is_cuda_oom(exc):
            raise
        logger.warning(
            "WhisperのCUDAメモリが不足したためCPUで再実行します"
        )
        model = None
        _release_cuda_cache()
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        lines, info = _run_whisper(
            model,
            vocals_path,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )
    logger.info("認識結果: %d行 (言語確度 %.2f)", len(lines), info.language_probability)
    for ln in lines:
        logger.debug("  [%.1f-%.1f] %s", ln.start_sec, ln.end_sec, ln.text)
    return lines
