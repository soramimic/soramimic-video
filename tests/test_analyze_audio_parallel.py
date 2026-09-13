import sys
import threading
from types import SimpleNamespace

from soramimic_video.audio_melody import MelodyNote
from soramimic_video.mora_align import AlignedMora
from soramimic_video.transcribe import TranscribedLine


def test_parallel_capacity_requires_cuda_and_free_memory_floor(monkeypatch):
    from soramimic_video import analyze_audio

    gib = 1024**3
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(mem_get_info=lambda device: (5800 * 1024**2, 16 * gib)),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert analyze_audio._has_parallel_cuda_capacity("cuda")
    assert not analyze_audio._has_parallel_cuda_capacity("cpu")

    fake_torch.cuda.mem_get_info = lambda device: (5800 * 1024**2 - 1, 16 * gib)
    assert not analyze_audio._has_parallel_cuda_capacity("cuda:0")


def test_sheetsage_and_whisper_overlap_after_separation(monkeypatch, tmp_path):
    from soramimic_video import analyze_audio as analyze_audio_module
    from soramimic_video import audio_melody, mora_align, pitch, reading, separation, transcribe

    separated = threading.Event()
    whisper_started = threading.Event()
    sheetsage_started = threading.Event()
    calls = []

    def separate(audio_path, output_dir):
        assert not whisper_started.is_set()
        assert not sheetsage_started.is_set()
        separated.set()
        return output_dir / "vocals.wav", output_dir / "no_vocals.wav"

    def recognize(path, model, device):
        assert separated.is_set()
        calls.append((path, model, device))
        whisper_started.set()
        assert sheetsage_started.wait(2), "SheetSage did not overlap Whisper"
        return [TranscribedLine(0.1, 0.5, "か")]

    def run_sheetsage(*args, **kwargs):
        assert separated.is_set()
        sheetsage_started.set()
        assert whisper_started.wait(2), "Whisper did not overlap SheetSage"
        return [MelodyNote(0.0, 0.6, 60)]

    monkeypatch.setattr(separation, "separate", separate)
    monkeypatch.setattr(transcribe, "transcribe_lines", recognize)
    monkeypatch.setattr(analyze_audio_module, "_has_parallel_cuda_capacity", lambda device: True)
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: ([AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8)], [0]),
    )
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: 0.4)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", run_sheetsage)
    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {"sheetsage2": True, "rmvpe": False, "fcpe": False},
    )

    project = analyze_audio_module.analyze_audio(
        tmp_path / "input.wav",
        tmp_path / "project",
        device="cuda",
    )

    assert calls == [
        (
            tmp_path / "project/separation/vocals.wav",
            "large-v3",
            "cuda",
        )
    ]
    assert [(note.kana, note.midi_note) for note in project.notes] == [("カ", 60)]


def test_low_memory_keeps_whisper_and_sheetsage_serial(monkeypatch, tmp_path):
    from soramimic_video import analyze_audio as analyze_audio_module
    from soramimic_video import audio_melody, mora_align, pitch, reading, transcribe

    calls = []
    monkeypatch.setattr(
        analyze_audio_module,
        "_has_parallel_cuda_capacity",
        lambda device: False,
    )
    monkeypatch.setattr(
        transcribe,
        "transcribe_lines",
        lambda *args, **kwargs: calls.append("whisper") or [TranscribedLine(0.1, 0.5, "か")],
    )
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: (
            [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8)],
            [0],
        ),
    )
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: 0.4)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60])
    monkeypatch.setattr(
        audio_melody,
        "transcribe_sheetsage",
        lambda *args, **kwargs: calls.append("sheetsage") or [MelodyNote(0.0, 0.6, 60)],
    )
    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {
            "sheetsage2": True,
            "rmvpe": False,
            "fcpe": False,
        },
    )

    analyze_audio_module.analyze_audio(
        tmp_path / "input.wav",
        tmp_path / "project",
        device="cuda",
        skip_separation=True,
    )

    assert calls == ["whisper", "sheetsage"]
