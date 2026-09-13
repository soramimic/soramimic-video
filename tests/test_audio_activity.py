import numpy as np
import pytest

from soramimic_video.audio_activity import (
    ActivityInterval,
    activity_intervals,
    activity_overlap_seconds,
)


def test_activity_intervals_detects_sound_and_bridges_short_gap():
    sample_rate = 1000
    audio = np.zeros(2000, dtype=np.float32)
    audio[400:700] = 0.5
    audio[800:1100] = 0.5

    intervals = activity_intervals(audio, sample_rate)

    assert len(intervals) == 1
    assert intervals[0].start_sec == pytest.approx(0.375)
    assert intervals[0].end_sec == pytest.approx(1.125)


def test_activity_intervals_handles_stereo_and_true_silence():
    silent = np.zeros((1000, 2), dtype=np.float32)
    assert activity_intervals(silent, 1000) == []
    silent[500:600, 1] = 0.2
    assert activity_intervals(silent, 1000) == [ActivityInterval(0.475, 0.625)]


def test_activity_overlap_seconds_uses_union_intervals():
    intervals = [ActivityInterval(1.0, 2.0), ActivityInterval(3.0, 4.0)]
    assert activity_overlap_seconds(1.5, 3.25, intervals) == pytest.approx(0.75)
    assert activity_overlap_seconds(5.0, 4.0, intervals) == 0.0


def test_activity_intervals_rejects_invalid_input():
    with pytest.raises(ValueError, match="sample_rate"):
        activity_intervals(np.zeros(4), 0)
    with pytest.raises(ValueError, match="top_db"):
        activity_intervals(np.zeros(4), 1000, top_db=0)
    with pytest.raises(ValueError, match="mono"):
        activity_intervals(np.zeros((2, 2, 2)), 1000)
