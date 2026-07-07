"""Whisper による歌詞認識(元歌詞テキストが無いときのフォールバック)。

faster-whisper のセグメントをそのまま「行」として扱う。
認識誤りは替え歌変換の入力誤りとして伝播するため、
元歌詞がある場合は analyze-audio に --lyrics で渡すこと。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WHISPER_MODEL = "large-v3"


@dataclass
class TranscribedLine:
    start_sec: float
    end_sec: float
    text: str


def transcribe_lines(
    vocals_path: Path,
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = "auto",
) -> list[TranscribedLine]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper がインストールされていません(uv sync --extra audio)"
        ) from e

    logger.info("Whisper(%s)で歌詞を認識中...", model_size)
    model = WhisperModel(model_size, device=device)
    segments, info = model.transcribe(
        str(vocals_path), language="ja", vad_filter=True
    )
    lines = [
        TranscribedLine(start_sec=s.start, end_sec=s.end, text=s.text.strip())
        for s in segments
        if s.text.strip()
    ]
    logger.info("認識結果: %d行 (言語確度 %.2f)", len(lines), info.language_probability)
    for ln in lines:
        logger.debug("  [%.1f-%.1f] %s", ln.start_sec, ln.end_sec, ln.text)
    return lines
