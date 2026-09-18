from soramimic_video.audio_melody import MelodyNote
from soramimic_video.stage3 import _snap_whisper_windows_to_sheetsage_rests


def test_whisper_windows_snap_to_nearest_eligible_sheetsage_rests():
    windows = [(0.0, 1.0), (1.1, 2.0), (2.1, 3.0)]
    notes = [
        MelodyNote(0.1, 0.8, 60),
        MelodyNote(1.0, 1.5, 62),
        MelodyNote(1.55, 2.0, 64),
        MelodyNote(2.2, 2.8, 65),
    ]

    assert _snap_whisper_windows_to_sheetsage_rests(windows, notes) == (
        (0.0, 0.9),
        (0.9, 2.1),
        (2.1, 3.0),
    )


def test_whisper_windows_ignore_short_or_distant_sheetsage_rests():
    windows = [(0.0, 1.0), (1.1, 2.0)]
    notes = [
        MelodyNote(0.0, 0.50, 60),
        MelodyNote(0.57, 0.90, 62),
        MelodyNote(3.0, 3.3, 64),
    ]

    assert _snap_whisper_windows_to_sheetsage_rests(windows, notes) == tuple(windows)
