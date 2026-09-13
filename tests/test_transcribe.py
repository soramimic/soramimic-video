import sys
from pathlib import Path
from types import SimpleNamespace

from soramimic_video import transcribe as transcribe_module
from soramimic_video.transcribe import transcribe_lines


def test_transcribe_lines_exposes_context_and_vad_choices_and_keeps_defaults(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, *, device):
            calls.append(("model", model, device))

        def transcribe(self, path, **kwargs):
            calls.append((
                "run",
                path,
                kwargs["language"],
                kwargs["vad_filter"],
                kwargs["condition_on_previous_text"],
            ))
            return iter([
                SimpleNamespace(start=1.0, end=2.0, text="  一行目 "),
                SimpleNamespace(start=2.0, end=3.0, text="   "),
            ]), SimpleNamespace(language_probability=0.9)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    lines = transcribe_lines(
        Path("mix.wav"),
        "small",
        "cpu",
        vad_filter=False,
        condition_on_previous_text=False,
    )
    assert [(line.start_sec, line.end_sec, line.text) for line in lines] == [
        (1.0, 2.0, "一行目"),
    ]
    assert calls[-1] == ("run", "mix.wav", "ja", False, False)

    transcribe_lines(Path("vocals.wav"), "small", "cpu")
    assert calls[-1] == ("run", "vocals.wav", "ja", True, True)


def test_transcribe_lines_uses_cpu_when_cuda_memory_is_low(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, **kwargs):
            calls.append(("model", model, kwargs))

        def transcribe(self, path, **kwargs):
            return iter([SimpleNamespace(start=0.0, end=1.0, text=" 歌詞 ")]), SimpleNamespace(
                language_probability=0.8
            )

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_cuda_free_bytes", lambda device: 4 * 1024**3)

    lines = transcribe_lines(Path("vocals.wav"), device="auto")

    assert [line.text for line in lines] == ["歌詞"]
    assert calls == [
        ("model", "large-v3", {"device": "cpu", "compute_type": "int8"})
    ]


def test_transcribe_lines_retries_cuda_oom_on_cpu(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, **kwargs):
            self.device = kwargs["device"]
            calls.append(("model", model, kwargs))

        def transcribe(self, path, **kwargs):
            if self.device != "cpu":
                def failed_segments():
                    raise RuntimeError("CUDA failed with error out of memory")
                    yield

                return failed_segments(), SimpleNamespace(language_probability=0.0)
            return iter([SimpleNamespace(start=0.0, end=1.0, text=" 歌詞 ")]), SimpleNamespace(
                language_probability=0.8
            )

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_cuda_free_bytes", lambda device: 8 * 1024**3)
    monkeypatch.setattr(
        transcribe_module,
        "_release_cuda_cache",
        lambda: calls.append(("release",)),
    )

    lines = transcribe_lines(Path("vocals.wav"), device="cuda")

    assert [line.text for line in lines] == ["歌詞"]
    assert calls == [
        ("model", "large-v3", {"device": "cuda"}),
        ("release",),
        ("model", "large-v3", {"device": "cpu", "compute_type": "int8"}),
    ]


def test_transcribe_lines_does_not_hide_non_oom_cuda_errors(monkeypatch):
    class Whisper:
        def __init__(self, model, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            raise RuntimeError("CUDA driver is unavailable")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_cuda_free_bytes", lambda device: 8 * 1024**3)

    try:
        transcribe_lines(Path("vocals.wav"), device="cuda")
    except RuntimeError as exc:
        assert str(exc) == "CUDA driver is unavailable"
    else:
        raise AssertionError("non-OOM CUDA errors must remain visible")
