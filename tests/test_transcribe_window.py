import numpy as np
import soundfile as sf

from soramimic_video import transcribe
from soramimic_video.transcribe import TranscribedLine


def test_transcribe_window_crops_audio_and_restores_absolute_times(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    sf.write(audio, np.zeros((1000, 2), dtype=np.float32), 100)
    observed = {}

    def recognize(path, model_size, device, **kwargs):
        info = sf.info(path)
        observed.update(
            frames=info.frames,
            model_size=model_size,
            device=device,
            options=kwargs,
        )
        return [TranscribedLine(0.2, 1.4, "歌")]

    monkeypatch.setattr(transcribe, "transcribe_lines", recognize)

    lines = transcribe.transcribe_window(audio, 2.0, 3.0, "large-v3", "cpu")

    assert observed == {
        "frames": 100,
        "model_size": "large-v3",
        "device": "cpu",
        "options": {"vad_filter": False, "condition_on_previous_text": False},
    }
    assert lines == [TranscribedLine(2.2, 3.0, "歌")]
