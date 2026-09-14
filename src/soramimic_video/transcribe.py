"""Whisper による歌詞認識(元歌詞テキストが無いときのフォールバック)。

faster-whisper のセグメントをそのまま「行」として扱う。
認識誤りは替え歌変換の入力誤りとして伝播するため、
元歌詞がある場合は analyze-audio に --lyrics で渡すこと。
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WHISPER_MODEL = "large-v3"
_MIN_WHISPER_CUDA_FREE_BYTES = 5800 * 1024**2
_WHISPER_MODEL_CACHE: dict[tuple[str, str, str | None], Any] = {}


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
    cancel_check: Callable[[], Any] | None = None,
) -> tuple[list[TranscribedLine], Any]:
    segments, info = whisper_model.transcribe(
        str(vocals_path),
        language="ja",
        vad_filter=vad_filter,
        condition_on_previous_text=condition_on_previous_text,
    )
    lines = []
    for segment in segments:
        if cancel_check is not None:
            cancel_check()
        text = segment.text.strip()
        if text:
            lines.append(
                TranscribedLine(
                    start_sec=segment.start,
                    end_sec=segment.end,
                    text=text,
                )
            )
    return lines, info


def _load_whisper_model(
    model_size: str,
    device: str,
    compute_type: str | None,
    *,
    cache_model: bool,
):
    from faster_whisper import WhisperModel

    key = (model_size, device, compute_type)
    if cache_model and key in _WHISPER_MODEL_CACHE:
        return _WHISPER_MODEL_CACHE[key]
    model_kwargs = {"device": device}
    if compute_type is not None:
        model_kwargs["compute_type"] = compute_type
    model = WhisperModel(model_size, **model_kwargs)
    if cache_model:
        _WHISPER_MODEL_CACHE[key] = model
    return model


def transcribe_lines(
    vocals_path: Path,
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = "auto",
    *,
    vad_filter: bool = True,
    condition_on_previous_text: bool = True,
) -> list[TranscribedLine]:
    from .audio_inference import configured_url, transcribe_lines_remote

    if configured_url() is not None:
        logger.info("共有Whisperサービスで歌詞を認識中...")
        return transcribe_lines_remote(
            vocals_path,
            model_size,
            device,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )
    return _transcribe_lines_local(
        vocals_path,
        model_size,
        device,
        vad_filter=vad_filter,
        condition_on_previous_text=condition_on_previous_text,
    )


def _transcribe_lines_local(
    vocals_path: Path,
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = "auto",
    *,
    vad_filter: bool = True,
    condition_on_previous_text: bool = True,
    cache_model: bool = False,
    cuda_capacity_reserved: bool = False,
    cancel_check: Callable[[], Any] | None = None,
) -> list[TranscribedLine]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper がインストールされていません(uv sync --extra audio)"
        ) from e

    requested_device = device
    free_bytes = _cuda_free_bytes(requested_device)
    compute_type: str | None = None
    if (
        not cuda_capacity_reserved
        and free_bytes is not None
        and free_bytes < _MIN_WHISPER_CUDA_FREE_BYTES
    ):
        logger.warning(
            "GPU空き容量が%.1fGiBのためWhisperをCPUで実行します",
            free_bytes / 1024**3,
        )
        device = "cpu"
        compute_type = "int8"

    logger.info("Whisper(%s, %s)で歌詞を認識中...", model_size, device)
    try:
        model = _load_whisper_model(
            model_size, device, compute_type, cache_model=cache_model,
        )
        lines, info = _run_whisper(
            model,
            vocals_path,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            cancel_check=cancel_check,
        )
    except RuntimeError as exc:
        if device == "cpu" or not _is_cuda_oom(exc):
            raise
        logger.warning(
            "WhisperのCUDAメモリが不足したためCPUで再実行します"
        )
        if cache_model:
            _WHISPER_MODEL_CACHE.pop((model_size, device, compute_type), None)
        model = None
        _release_cuda_cache()
        model = _load_whisper_model(
            model_size, "cpu", "int8", cache_model=cache_model,
        )
        lines, info = _run_whisper(
            model,
            vocals_path,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            cancel_check=cancel_check,
        )
    logger.info("認識結果: %d行 (言語確度 %.2f)", len(lines), info.language_probability)
    for ln in lines:
        logger.debug("  [%.1f-%.1f] %s", ln.start_sec, ln.end_sec, ln.text)
    return lines
