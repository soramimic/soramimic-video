"""Relative vocal-stem activity evidence for automatic transcript screening."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

FRAME_DURATION_SEC = 0.05
ACTIVE_FRAME_FLOOR_DBFS = -70.0
LOUD_FRAME_PERCENTILE = 90.0
MAX_RELATIVE_DROP_DB = 30.0
_DBFS_FLOOR = -240.0


@dataclass(frozen=True)
class VocalActivityLine:
    """Activity evidence measured within one Whisper line window."""

    percentile_dbfs: float
    relative_db: float
    active_frame_ratio: float
    supported: bool


@dataclass(frozen=True)
class VocalActivityProfile:
    """Song-relative activity reference and per-line evidence."""

    reference_dbfs: float
    lines: tuple[VocalActivityLine, ...]


def _frame_dbfs(samples: np.ndarray, frame_samples: int) -> np.ndarray:
    """Return channel-inclusive RMS dBFS for consecutive frames."""
    if samples.ndim != 2:
        raise ValueError("ボーカル音源はチャンネル次元を含む必要があります")
    if frame_samples <= 0:
        raise ValueError("ボーカル音量フレーム長が不正です")
    levels: list[float] = []
    for start in range(0, len(samples), frame_samples):
        frame = samples[start : start + frame_samples]
        if not len(frame):
            continue
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        levels.append(max(_DBFS_FLOOR, 20.0 * np.log10(max(rms, 1e-12))))
    return np.asarray(levels, dtype=np.float64)


def measure_vocal_activity(
    vocals_path: Path,
    windows: list[tuple[float, float]],
) -> VocalActivityProfile:
    """Measure whether each line contains sustained song-relative stem energy.

    A 90th-percentile frame level represents whether at least a meaningful fraction
    of the line has vocal-stem energy. This avoids allowing one short residual peak
    to validate an otherwise silent, long Whisper segment.
    """
    import soundfile as sf

    samples, sample_rate = sf.read(
        vocals_path,
        dtype="float32",
        always_2d=True,
    )
    if sample_rate <= 0 or not len(samples):
        raise ValueError("ボーカル音源が空です")
    if not np.isfinite(samples).all():
        raise ValueError("ボーカル音源に非有限値が含まれています")
    frame_samples = max(1, round(sample_rate * FRAME_DURATION_SEC))
    song_levels = _frame_dbfs(samples, frame_samples)
    active_levels = song_levels[song_levels >= ACTIVE_FRAME_FLOOR_DBFS]
    reference_levels = active_levels if len(active_levels) else song_levels
    reference_dbfs = float(np.percentile(reference_levels, LOUD_FRAME_PERCENTILE))
    support_floor = reference_dbfs - MAX_RELATIVE_DROP_DB

    evidence: list[VocalActivityLine] = []
    for start_sec, end_sec in windows:
        start = max(0, min(len(samples), round(start_sec * sample_rate)))
        end = max(start, min(len(samples), round(end_sec * sample_rate)))
        levels = _frame_dbfs(samples[start:end], frame_samples)
        percentile_dbfs = (
            float(np.percentile(levels, LOUD_FRAME_PERCENTILE))
            if len(levels)
            else _DBFS_FLOOR
        )
        evidence.append(VocalActivityLine(
            percentile_dbfs=percentile_dbfs,
            relative_db=percentile_dbfs - reference_dbfs,
            active_frame_ratio=(
                float(np.mean(levels >= support_floor)) if len(levels) else 0.0
            ),
            supported=percentile_dbfs >= support_floor,
        ))
    return VocalActivityProfile(reference_dbfs, tuple(evidence))
