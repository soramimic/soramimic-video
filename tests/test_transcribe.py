import sys
from pathlib import Path
from types import SimpleNamespace

from soramimic_video import transcribe as transcribe_module
from soramimic_video.transcribe import transcribe_lines


def test_transcribe_lines_exposes_context_and_vad_choices_and_keeps_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(transcribe_module, "_audio_duration_sec", lambda _path: 10.0)

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
        language="en",
        vad_filter=False,
        condition_on_previous_text=False,
    )
    assert [(line.start_sec, line.end_sec, line.text) for line in lines] == [
        (1.0, 2.0, "一行目"),
    ]
    assert calls[-1] == ("run", "mix.wav", "en", False, False)

    transcribe_lines(Path("vocals.wav"), "small", "cpu")
    assert calls[-1] == ("run", "vocals.wav", "ja", True, True)

    transcribe_lines(Path("auto.wav"), "small", "cpu", language=None)
    assert calls[-1] == ("run", "auto.wav", None, True, True)


def test_transcribe_lines_uses_cpu_when_cuda_memory_is_low(monkeypatch):
    calls = []
    monkeypatch.setattr(transcribe_module, "_audio_duration_sec", lambda _path: 10.0)

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
    monkeypatch.setattr(transcribe_module, "_audio_duration_sec", lambda _path: 10.0)

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
    monkeypatch.setattr(transcribe_module, "_audio_duration_sec", lambda _path: 10.0)

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


def test_transcribe_lines_clamps_segments_to_physical_audio_duration(monkeypatch):
    class Whisper:
        def __init__(self, model, *, device):
            pass

        def transcribe(self, path, **kwargs):
            return iter([
                SimpleNamespace(start=-0.5, end=0.5, text=" 先頭 "),
                SimpleNamespace(start=0.0, end=29.98, text=" 正常入力 "),
                SimpleNamespace(start=12.623, end=29.98, text=" 空区間 "),
                SimpleNamespace(start=13.0, end=14.0, text=" 範囲外 "),
            ]), SimpleNamespace(language_probability=0.9)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_audio_duration_sec", lambda _path: 12.623)

    lines = transcribe_lines(Path("short.wav"), "small", "cpu")

    assert [(line.start_sec, line.end_sec, line.text) for line in lines] == [
        (0.0, 0.5, "先頭"),
        (0.0, 12.623, "正常入力"),
    ]


def test_shared_server_reuses_whisper_model(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

        def transcribe(self, path, **kwargs):
            return iter([]), SimpleNamespace(language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_cuda_free_bytes", lambda device: None)
    transcribe_module._WHISPER_MODEL_CACHE.clear()

    for _ in range(2):
        transcribe_module._transcribe_lines_local(
            Path("vocals.wav"),
            "large-v3",
            "cpu",
            cache_model=True,
        )

    assert calls == [("large-v3", {"device": "cpu"})]
    transcribe_module._WHISPER_MODEL_CACHE.clear()


def test_shared_gpu_reservation_avoids_conflicting_free_memory_fallback(monkeypatch):
    calls = []

    class Whisper:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

        def transcribe(self, path, **kwargs):
            return iter([]), SimpleNamespace(language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(transcribe_module, "_cuda_free_bytes", lambda device: 1024**3)

    transcribe_module._transcribe_lines_local(
        Path("vocals.wav"),
        "large-v3",
        "cuda",
        cuda_capacity_reserved=True,
    )

    assert calls == [("large-v3", {"device": "cuda"})]
