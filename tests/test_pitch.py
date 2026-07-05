import pytest

np = pytest.importorskip("numpy")

from soramimic_video.pitch import PitchTrack, mora_midi_notes, voiced_end  # noqa: E402


def _track(midi_values: list[float], period: float = 0.1) -> PitchTrack:
    times = np.arange(len(midi_values)) * period
    return PitchTrack(times=times, midi=np.array(midi_values, dtype=float))


def test_mora_midi_notes_median():
    track = _track([60.0, 60.2, 59.8, 67.0, 67.2, np.nan])
    notes = mora_midi_notes(track, [(0.0, 0.3), (0.3, 0.5)])
    assert notes == [60, 67]


def test_mora_midi_notes_fallback_to_neighbor():
    track = _track([64.0, 64.0, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan])
    notes = mora_midi_notes(track, [(0.0, 0.2), (0.4, 0.8)])
    assert notes == [64, 64]  # 無声区間は直前のモーラの音高


def test_mora_midi_notes_all_unvoiced_uses_default():
    track = _track([np.nan, np.nan])
    assert mora_midi_notes(track, [(0.0, 0.2)], default=62) == [62]


def test_voiced_end_stops_at_unvoiced_break():
    # 0.5秒まで有声、その後3フレーム以上無声
    track = _track([60.0] * 5 + [np.nan] * 5)
    end = voiced_end(track, 0.0, limit_sec=1.0)
    assert end == pytest.approx(0.5)


def test_voiced_end_reaches_limit_when_voiced():
    track = _track([60.0] * 10)
    assert voiced_end(track, 0.0, limit_sec=0.8) == 0.8


def test_voiced_end_ignores_short_gap():
    track = _track([60.0, 60.0, np.nan, 60.0, 60.0, 60.0])
    assert voiced_end(track, 0.0, limit_sec=0.6) == 0.6
