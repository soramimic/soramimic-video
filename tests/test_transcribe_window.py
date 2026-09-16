import sys
from types import SimpleNamespace

import numpy as np

from soramimic_video import transcribe
from soramimic_video.transcribe import TranscribedLine


def test_transcribe_window_crops_audio_and_restores_absolute_times(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    observed = {}
    writes = {}

    class Source:
        samplerate = 100

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __len__(self):
            return 1000

        def seek(self, frame):
            observed["seek"] = frame

        def read(self, frames, **_kwargs):
            return np.zeros((frames, 2), dtype=np.float32)

    def write(path, samples, samplerate, **kwargs):
        writes[str(path)] = (samples, samplerate, kwargs)

    fake_soundfile = SimpleNamespace(
        SoundFile=lambda _path: Source(),
        write=write,
    )
    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)

    def recognize(path, model_size, device, **kwargs):
        samples, samplerate, write_options = writes[str(path)]
        observed.update(
            frames=len(samples),
            samplerate=samplerate,
            write_options=write_options,
            model_size=model_size,
            device=device,
            options=kwargs,
        )
        return [TranscribedLine(0.2, 1.4, "歌")]

    monkeypatch.setattr(transcribe, "transcribe_lines", recognize)

    lines = transcribe.transcribe_window(audio, 2.0, 3.0, "large-v3", "cpu")

    assert observed == {
        "seek": 200,
        "frames": 100,
        "samplerate": 100,
        "write_options": {"subtype": "FLOAT"},
        "model_size": "large-v3",
        "device": "cpu",
        "options": {"vad_filter": False, "condition_on_previous_text": False},
    }
    assert lines == [TranscribedLine(2.2, 3.0, "歌")]
