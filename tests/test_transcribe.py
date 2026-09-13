import sys
from pathlib import Path
from types import SimpleNamespace

from soramimic_video.transcribe import transcribe_lines


def test_transcribe_lines_exposes_vad_choice_and_keeps_default(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, *, device):
            calls.append(("model", model, device))

        def transcribe(self, path, **kwargs):
            calls.append(("run", path, kwargs["language"], kwargs["vad_filter"]))
            return iter([
                SimpleNamespace(start=1.0, end=2.0, text="  一行目 "),
                SimpleNamespace(start=2.0, end=3.0, text="   "),
            ]), SimpleNamespace(language_probability=0.9)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    lines = transcribe_lines(Path("mix.wav"), "small", "cpu", vad_filter=False)
    assert [(line.start_sec, line.end_sec, line.text) for line in lines] == [
        (1.0, 2.0, "一行目"),
    ]
    assert calls[-1] == ("run", "mix.wav", "ja", False)

    transcribe_lines(Path("vocals.wav"), "small", "cpu")
    assert calls[-1] == ("run", "vocals.wav", "ja", True)
