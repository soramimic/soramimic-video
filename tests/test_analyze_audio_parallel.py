import sys
import threading
from types import SimpleNamespace

from soramimic_video.audio_melody import MelodyNote
from soramimic_video.mora_align import AlignedMora
from soramimic_video.transcribe import TranscribedLine


def test_local_model_device_prefers_cuda_when_available(monkeypatch):
    from soramimic_video import analyze_audio

    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert analyze_audio._torch_device(None) == "cuda"
    assert analyze_audio._torch_device("cpu") == "cpu"


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

    result = analyze_audio._run_audio_models(
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

    result = analyze_audio._run_audio_models(
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


def test_kana_evidence_reranks_closed_candidates_from_mix_and_vocals(
    monkeypatch,
    tmp_path,
):
    from soramimic_video import analyze_audio, kana_whisper

    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=10.0)),
    )
    mix = tmp_path / "mix.wav"
    vocals = tmp_path / "vocals.wav"

    def transcribe(path, windows, device):
        assert windows == [(0.0, 5.5)]
        assert device == "auto"
        return [
            "ウタビアフレテナニオシテイタノ" if path == mix else "ウタビアフレテナニモシテイタノ"
        ]

    monkeypatch.setattr(kana_whisper, "transcribe_kana_windows", transcribe)
    chosen, receipt = analyze_audio._choose_readings_with_kana(
        mix,
        vocals,
        ["何をしていたの"],
        [
            [
                ["ナ", "ン", "オ", "シ", "テ", "イ", "タ", "ノ"],
                ["ナ", "ニ", "オ", "シ", "テ", "イ", "タ", "ノ"],
            ]
        ],
        [(1.5, 4.0)],
        device="auto",
        shared_inference=True,
    )

    assert chosen == [1]
    assert receipt["sources"] == ["original-mix", "separated-vocals"]
    assert receipt["lines"][0]["selected_index"] == 1


def test_kana_choice_keeps_candidates_with_different_mora_counts():
    from soramimic_video import analyze_audio

    assert analyze_audio._has_kana_choice(
        [[
            ["シャ", "ウ", "ト", "イ", "ッ", "ト", "ア", "ウ", "ト"],
            ["シャ", "ウ", "ティ", "タ", "ウ", "ト"],
        ]]
    )


def test_audio_pipeline_prefetches_all_shared_models(monkeypatch, tmp_path):
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        audio_melody,
        mora_align,
        reading,
        vocal_activity,
    )
    from soramimic_video.vocal_activity import (
        VocalActivityLine,
        VocalActivityProfile,
    )

    calls = []
    shared_notes = [MelodyNote(0.6, 0.8, 60)]

    def run_shared(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            tmp_path / "project/separation/vocals.wav",
            tmp_path / "project/separation/no_vocals.wav",
            [TranscribedLine(0.1, 0.5, "かき")],
            shared_notes,
        )

    monkeypatch.setenv(
        "SORAMIMIC_AUDIO_INFERENCE_URL",
        "http://127.0.0.1:8320",
    )
    monkeypatch.setattr(analyze_audio_module, "_require_audio_pipeline", lambda: None)
    monkeypatch.setattr(analyze_audio_module, "_run_audio_models", run_shared)
    monkeypatch.setattr(
        vocal_activity,
        "measure_vocal_activity",
        lambda *_args, **_kwargs: VocalActivityProfile(
            -15.0,
            (VocalActivityLine(-20.0, -5.0, 0.8, True),),
        ),
    )
    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {"sheetsage2": True},
    )
    monkeypatch.setattr(reading, "reading_candidates", lambda _text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: (
            [
                AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8),
                AlignedMora(0, 1, "キ", 0.3, 0.4, 0.8),
            ],
            [0],
        ),
    )

    class Document:
        def to_json(self):
            return '{"note_candidates": [], "links": []}'

    class Layers:
        def to_dict(self):
            return {
                "schema_version": 1,
                "canonical_text": "かき",
                "canonical": [{
                    "utterance_id": "u0", "text": "かき", "kana": "カキ",
                    "mora_ids": ["m0", "m1"],
                }],
                "performed": [
                    {
                        "singing_unit_id": "s0", "mora_ids": ["m0"],
                        "status": "weak", "start_sec": 0.1, "end_sec": 0.2,
                        "confidence": 0.8, "link_ids": ["l0"],
                        "evidence_ids": [],
                    },
                    {
                        "singing_unit_id": "s1", "mora_ids": ["m1"],
                        "status": "weak", "start_sec": 0.6, "end_sec": 0.8,
                        "confidence": 0.8, "link_ids": ["l1"],
                        "evidence_ids": [],
                    },
                ],
                "synthesis_plan": [{
                    "id": "slot-1", "utterance_id": "u0",
                    "singing_unit_id": "s1", "mora_ids": ["m1"],
                    "note_candidate_id": "n1", "link_ids": ["l1"],
                    "kana": "キ", "start_sec": 0.6, "end_sec": 0.8,
                    "midi_pitch": 64, "operation": "match",
                    "timing_source": "note_interval", "confidence": 0.0,
                    "evidence_ids": [], "pitch_sources": ["sheetsage2-vocal"],
                    "pitch_confidence": None, "continuation": False,
                }],
                "omissions": [], "unresolved_unit_ids": ["s0"],
                "diagnostics": [], "evidence": [],
            }

    monkeypatch.setitem(
        sys.modules,
        "soramimic_video.stage3",
        SimpleNamespace(build_stage3_layers=lambda *_args, **_kwargs: (
            Document(), Layers()
        )),
    )

    project = analyze_audio_module.analyze_audio(
        tmp_path / "input.wav",
        tmp_path / "project",
        device="cuda",
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
    assert recognition["semantic_gate"]["vocal_activity"] == {
        "applied": True,
        "source": "demucs-separated-vocals",
        "frame_duration_sec": 0.05,
        "line_percentile": 90.0,
        "active_frame_floor_dbfs": -70.0,
        "max_relative_drop_db": 30.0,
        "reference_dbfs": -15.0,
    }
    assert [item["status"] for item in json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text()
    )["diagnostics"]] == [
        "unresolved", "spoken-synthesis-recovery", "spoken-continuous-timing",
    ]
    assert [note.kana for note in project.notes] == ["カ", "キ"]
    assert project.notes[0].midi_note == 60
    assert project.notes[0].source == "spoken"
    assert project.lyric_layers["omissions"] == []
