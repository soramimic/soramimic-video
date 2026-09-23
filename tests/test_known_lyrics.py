from types import SimpleNamespace

import pytest

from soramimic_video import known_lyrics, vocal_activity
from soramimic_video.audio_melody import MelodyNote
from soramimic_video.transcribe import TranscribedLine


def test_known_lyrics_adjustment_drops_inactive_recognition(monkeypatch, tmp_path):
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: text)
    monkeypatch.setattr(vocal_activity, "measure_vocal_activity", lambda *_: SimpleNamespace(
        lines=[SimpleNamespace(supported=True), SimpleNamespace(supported=False)],
    ))
    lines, audit = known_lyrics.adjust_supplied_lines(
        ["かき", "さし"],
        [TranscribedLine(0, 1, "かき"), TranscribedLine(1, 2, "さし")],
        [MelodyNote(0, 2, 60)], vocals=tmp_path / "vocals.wav",
    )
    assert lines == ["かき"]
    assert audit["supplied_lines"] == ["かき", "さし"]


def test_known_lyrics_adjustment_reports_unrelated_audio(monkeypatch):
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: text)
    with pytest.raises(RuntimeError, match="削除・補完をオフ"):
        known_lyrics.adjust_supplied_lines(
            ["かき"], [TranscribedLine(0, 1, "さし")], [MelodyNote(0, 1, 60)], vocals=None,
        )
