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


def test_shared_demucs_whisper_and_sheetsage_overlap(monkeypatch, tmp_path):
    from soramimic_video import analyze_audio, audio_melody, separation, transcribe

    started = set()
    lock = threading.Lock()
    all_started = threading.Event()

    def mark_started(kind):
        with lock:
            started.add(kind)
            if started == {"demucs", "whisper", "sheetsage"}:
                all_started.set()
        assert all_started.wait(2), f"{kind} did not overlap the other models"

    def separate(_audio, output_dir):
        mark_started("demucs")
        return output_dir / "vocals.wav", output_dir / "no_vocals.wav"

    def whisper(*args, **kwargs):
        mark_started("whisper")
        assert kwargs == {
            "vad_filter": False,
            "condition_on_previous_text": False,
        }
        return [TranscribedLine(0.1, 0.5, "か")]

    def sheetsage(*args, **kwargs):
        mark_started("sheetsage")
        return [MelodyNote(0.0, 0.6, 60)]

    monkeypatch.setattr(separation, "separate", separate)
    monkeypatch.setattr(transcribe, "transcribe_lines", whisper)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", sheetsage)

    result = analyze_audio._run_evidence_models(
        tmp_path / "input.wav",
        tmp_path / "project",
        "large-v3",
        "auto",
        "auto",
        lambda _value: None,
        run_separation=True,
        run_whisper=True,
        shared_inference=True,
    )

    assert result == (
        tmp_path / "project/separation/vocals.wav",
        tmp_path / "project/separation/no_vocals.wav",
        [TranscribedLine(0.1, 0.5, "か")],
        [MelodyNote(0.0, 0.6, 60)],
    )


def test_known_lyrics_submit_demucs_and_sheetsage_without_whisper(
    monkeypatch, tmp_path,
):
    from soramimic_video import analyze_audio, audio_melody, separation, transcribe

    started = set()
    both_started = threading.Event()
    lock = threading.Lock()

    def mark(kind):
        with lock:
            started.add(kind)
            if started == {"demucs", "sheetsage"}:
                both_started.set()
        assert both_started.wait(2)

    def separate(_audio, output_dir):
        mark("demucs")
        return output_dir / "vocals.wav", output_dir / "no_vocals.wav"

    def sheetsage(*args, **kwargs):
        mark("sheetsage")
        return [MelodyNote(0.0, 0.6, 60)]

    monkeypatch.setattr(separation, "separate", separate)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", sheetsage)
    monkeypatch.setattr(
        transcribe,
        "transcribe_lines",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("known lyrics must bypass Whisper")
        ),
    )

    result = analyze_audio._run_evidence_models(
        tmp_path / "input.wav",
        tmp_path / "project",
        "large-v3",
        "auto",
        "auto",
        lambda _value: None,
        run_separation=True,
        run_whisper=False,
        shared_inference=True,
    )

    assert result[2] is None
    assert started == {"demucs", "sheetsage"}


def test_evidence_pipeline_prefetches_all_shared_models(monkeypatch, tmp_path):
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        audio_melody,
        mora_align,
        pitch,
        reading,
    )

    calls = []
    shared_notes = [MelodyNote(0.6, 0.8, 60)]

    def run_shared(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            tmp_path / "project/separation/vocals.wav",
            tmp_path / "project/separation/no_vocals.wav",
            [TranscribedLine(0.1, 0.5, "か")],
            shared_notes,
        )

    monkeypatch.setenv(
        "SORAMIMIC_AUDIO_INFERENCE_URL",
        "http://127.0.0.1:8320",
    )
    monkeypatch.setattr(analyze_audio_module, "_require_evidence_pipeline", lambda: None)
    monkeypatch.setattr(analyze_audio_module, "_run_evidence_models", run_shared)
    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {"sheetsage2": True, "rmvpe": False, "fcpe": False},
    )
    monkeypatch.setattr(reading, "reading_candidates", lambda _text: ["カ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: (
            [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8)],
            [0],
        ),
    )
    def forbidden(*_args, **_kwargs):
        raise AssertionError("evidence mode must not invoke pYIN pitch helpers")

    monkeypatch.setattr(pitch, "extract_pitch", forbidden)
    monkeypatch.setattr(pitch, "voiced_end", forbidden)
    monkeypatch.setattr(pitch, "mora_midi_notes", forbidden)
    monkeypatch.setattr(audio_melody, "extract_rmvpe", forbidden)
    monkeypatch.setattr(audio_melody, "extract_fcpe", forbidden)
    monkeypatch.setitem(sys.modules, "soramimic_video.audio_activity", None)

    def reject_stage3(*_args):
        raise ValueError("test fallback")

    monkeypatch.setitem(
        sys.modules,
        "soramimic_video.stage3",
        SimpleNamespace(build_stage3_layers=reject_stage3),
    )

    import pytest

    with pytest.raises(RuntimeError, match="歌詞や音高を補わず"):
        analyze_audio_module.analyze_audio(
            tmp_path / "input.wav",
            tmp_path / "project",
            device="cuda",
            lyric_pipeline="evidence",
        )

    assert len(calls) == 1
    assert calls[0][0][:5] == (
        tmp_path / "input.wav",
        tmp_path / "project",
        "large-v3",
        "cuda",
        "cuda",
    )
    assert calls[0][1] == {
        "run_separation": True,
        "run_whisper": True,
        "shared_inference": True,
    }
    import json

    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert recognition["semantic_gate"]["decisions"][0]["status"] == "unresolved"
    assert [item["status"] for item in json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text()
    )["diagnostics"]] == ["unresolved", "unresolved"]


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
