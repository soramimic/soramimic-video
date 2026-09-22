import sys
from types import SimpleNamespace

import numpy as np
import pytest

from soramimic_video.vocal_activity import measure_vocal_activity


def test_measure_vocal_activity_uses_sustained_relative_stem_level(
    monkeypatch, tmp_path,
):
    sample_rate = 100
    # At 50 ms per frame these are 5-sample frames. Most of the song establishes
    # a -20 dBFS vocal reference. The last line has one loud frame among twenty,
    # which must not validate the otherwise silent interval.
    strong = np.full((400, 2), 0.1, dtype=np.float32)
    weak = np.full((100, 2), 0.0001, dtype=np.float32)
    weak[:5] = 0.1
    samples = np.concatenate([strong, weak])
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        read=lambda *_args, **_kwargs: (samples, sample_rate),
    ))

    profile = measure_vocal_activity(
        tmp_path / "vocals.wav",
        [(0.0, 1.0), (4.0, 5.0)],
    )

    assert profile.reference_dbfs == pytest.approx(-20.0, abs=0.01)
    assert profile.lines[0].supported is True
    assert profile.lines[0].active_frame_ratio == 1.0
    assert profile.lines[1].supported is False
    assert profile.lines[1].relative_db == pytest.approx(-60.0, abs=0.01)
    assert profile.lines[1].active_frame_ratio == pytest.approx(0.05)


def test_measure_vocal_activity_rejects_empty_audio(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        read=lambda *_args, **_kwargs: (np.empty((0, 2), dtype=np.float32), 48_000),
    ))

    with pytest.raises(ValueError, match="空"):
        measure_vocal_activity(tmp_path / "vocals.wav", [(0.0, 1.0)])
