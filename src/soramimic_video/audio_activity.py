"""Conservative audio-activity detection for rejecting silent ASR segments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ActivityInterval:
    start_sec: float
    end_sec: float


def activity_intervals(
    audio: np.ndarray,
    sample_rate: int,
    *,
    frame_ms: float = 50.0,
    hop_ms: float = 25.0,
    top_db: float = 70.0,
    bridge_silence_ms: float = 200.0,
) -> list[ActivityInterval]:
    """Return intervals whose short-time RMS is close to the track maximum.

    This detects only ``no energy`` versus ``some energy``. It does not try to
    classify singing as speech, so it cannot rewrite ASR text.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rateは正の値である必要があります")
    if top_db <= 0:
        raise ValueError("top_dbは正の値である必要があります")

    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 2:
        samples = np.max(np.abs(samples), axis=1)
    elif samples.ndim != 1:
        raise ValueError("audioはmonoまたは(sample, channel)配列である必要があります")
    samples = np.abs(samples)
    if samples.size == 0:
        return []

    frame = max(1, round(sample_rate * frame_ms / 1000.0))
    hop = max(1, round(sample_rate * hop_ms / 1000.0))
    starts = np.arange(0, samples.size, hop, dtype=np.int64)
    rms = np.empty(starts.size, dtype=np.float64)
    for index, start in enumerate(starts):
        chunk = samples[start : min(samples.size, start + frame)]
        rms[index] = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))

    peak = float(np.max(rms, initial=0.0))
    if peak <= np.finfo(np.float32).tiny:
        return []
    active_indices = np.flatnonzero(rms >= peak * (10.0 ** (-top_db / 20.0)))
    if active_indices.size == 0:
        return []

    bridge_frames = max(0, round(bridge_silence_ms / hop_ms))
    groups: list[tuple[int, int]] = []
    first = previous = int(active_indices[0])
    for raw_index in active_indices[1:]:
        index = int(raw_index)
        if index - previous - 1 <= bridge_frames:
            previous = index
            continue
        groups.append((first, previous))
        first = previous = index
    groups.append((first, previous))

    return [
        ActivityInterval(
            start_sec=float(starts[first]) / sample_rate,
            end_sec=float(min(samples.size, starts[last] + frame)) / sample_rate,
        )
        for first, last in groups
    ]


def detect_audio_activity(path: Path) -> list[ActivityInterval]:
    """Load an audio file and return conservative non-silent intervals."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError("音声区間検出にはsoundfileが必要です") from exc
    audio, sample_rate = sf.read(path, always_2d=True, dtype="float32")
    return activity_intervals(audio, int(sample_rate))


def activity_overlap_seconds(
    start_sec: float,
    end_sec: float,
    intervals: list[ActivityInterval],
) -> float:
    """Measure overlap with sorted, non-overlapping activity intervals."""
    if end_sec <= start_sec:
        return 0.0
    return sum(
        max(0.0, min(end_sec, interval.end_sec) - max(start_sec, interval.start_sec))
        for interval in intervals
    )
