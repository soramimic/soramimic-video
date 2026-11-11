"""Application policy for lyric-to-note search duration."""

from __future__ import annotations

import math
import os

SEARCH_TIMEOUT_ENV = "SORAMIMIC_AUDIO_SEARCH_TIMEOUT_SECONDS"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 120.0
SEARCH_TIMEOUT_MESSAGE = (
    "歌詞と音符の対応付けに時間がかかりすぎたため、生成を停止しました。"
    "音源と歌詞の範囲を確認して、もう一度お試しください。"
)


class AudioSearchTimeout(RuntimeError):
    """A search exceeded the application's configured duration limit."""

    def __init__(self, timeout_seconds: float, elapsed_seconds: float):
        super().__init__(SEARCH_TIMEOUT_MESSAGE)
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds


def configured_timeout() -> float:
    raw = os.environ.get(SEARCH_TIMEOUT_ENV, str(DEFAULT_SEARCH_TIMEOUT_SECONDS))
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ValueError(f"{SEARCH_TIMEOUT_ENV} must be positive and finite") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{SEARCH_TIMEOUT_ENV} must be positive and finite")
    return seconds
