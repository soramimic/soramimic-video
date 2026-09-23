"""Integration contract tests for the required Soramimic Score pipeline."""

import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest


def _ctc_emissions(kana="ラ", *event_times):
    from soramimic_video.mora_align import CTCEmissions

    matrix = np.full((100, 2), -8.0)
    matrix[:, 0] = -0.001
    for time_sec in event_times:
        frame = round((time_sec + .5) / .02)
        matrix[frame, 0] = -8.0
        matrix[frame, 1] = -0.001
    return CTCEmissions(matrix, {"<pad>": 0, kana: 1})


def test_stage3_uses_note_run_config_with_each_mora_ctc_peak(
    monkeypatch,
):
    from soramimic_score import pipeline

    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    captured = {}
    original = pipeline.run_stage3_document

    def run(document, *, config=None, **kwargs):
        captured["config"] = config
        captured["line_windows_by_utterance"] = kwargs.get(
            "line_windows_by_utterance"
        )
        captured["vocalization_reattacks_by_utterance"] = kwargs.get(
            "vocalization_reattacks_by_utterance"
        )
        return original(document, config=config, **kwargs)

    monkeypatch.setattr(pipeline, "run_stage3_document", run)

    document, realization = build_stage3_layers(
        ["かき"], ["カキ"],
        [AlignedMora(0, 0, "カ", 0.09, 0.11, 0.1),
         AlignedMora(0, 1, "キ", 0.39, 0.41, 0.1)],
        [MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62)],
        whisper_line_windows=[(0.0, 0.5)],
        enable_repeated_vocalization=True,
        ctc_emissions=_ctc_emissions("ラ"),
    )

    anchors = [item for item in document.evidence if item.kind == "mora-ctc-anchor"]
    assert [item.detail["time_sec"] for item in anchors] == pytest.approx([0.1, 0.4])
    from soramimic_score import NoteRunConfig

    assert captured["config"] == NoteRunConfig(whisper_boundary_cost_per_sec2=0.1)
    assert captured["line_windows_by_utterance"] == {"u0": (0.0, 0.5)}
    assert captured["vocalization_reattacks_by_utterance"] == {}
    notes = {item.id: item for item in document.note_candidates}
    assert [
        (item.kana, notes[item.note_candidate_id].midi_pitch)
        for item in realization.synthesis_plan
    ] == [
        ("カ", 60), ("キ", 62),
    ]
    assert not realization.unresolved_unit_ids


def test_stage3_disables_repeated_vocalization_without_whisper_windows(monkeypatch):
    from soramimic_score import pipeline

    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    captured = {}
    original = pipeline.run_stage3_document

    def run(document, *, config=None, **kwargs):
        captured.update(kwargs)
        return original(document, config=config, **kwargs)

    monkeypatch.setattr(pipeline, "run_stage3_document", run)
    build_stage3_layers(
        ["らら"], ["ララ"],
        [AlignedMora(0, 0, "ラ", 0.09, 0.11, 0.1),
         AlignedMora(0, 1, "ラ", 0.39, 0.41, 0.1)],
        [MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62)],
    )

    assert captured["line_windows_by_utterance"] is None
    assert captured["vocalization_reattacks_by_utterance"] is None


def test_stage3_requires_whisper_windows_for_repeated_vocalization():
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    with pytest.raises(ValueError, match="Whisper区間"):
        build_stage3_layers(
            ["らら"], ["ララ"],
            [AlignedMora(0, 0, "ラ", 0.09, 0.11, 0.1),
             AlignedMora(0, 1, "ラ", 0.39, 0.41, 0.1)],
            [MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62)],
            enable_repeated_vocalization=True,
        )


def test_stage3_expands_automatic_repeated_vocalization_from_raw_ctc_reattacks():
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    document, realization = build_stage3_layers(
        ["la la"], ["ララ"],
        [AlignedMora(0, 0, "ラ", 0.09, 0.11, 0.2),
         AlignedMora(0, 1, "ラ", 0.49, 0.51, 0.2)],
        [MelodyNote(0.0, 0.25, 60), MelodyNote(0.25, 0.5, 62),
         MelodyNote(0.5, 0.75, 64), MelodyNote(0.75, 1.0, 65)],
        whisper_line_windows=[(0.0, 1.0)],
        enable_repeated_vocalization=True,
        ctc_emissions=_ctc_emissions("ラ", 0.0, .25, .5, .75),
    )

    reading = next(
        item for item in document.readings
        if item.id == document.utterances[0].selected_reading_id
    )
    assert reading.kana == "ララララ"
    assert [item.kana for item in realization.synthesis_plan] == ["ラ"] * 4


def test_initial_pure_repetition_preserves_observed_attacks_without_local_retry(
    monkeypatch, tmp_path,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(
        transcribe,
        "transcribe_lines",
        lambda *a, **kw: [
            TranscribedLine(0.0, 4.0, "ラ" * 2),
            TranscribedLine(5.0, 6.0, "歌"),
        ],
    )

    def no_retry(*_args, **_kwargs):
        pytest.fail("初回から反復の行を局所再認識してはいけない")

    monkeypatch.setattr(transcribe, "transcribe_window", no_retry)
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: {"ラ" * 2: ["ラ" * 2], "歌": ["ウタ"]}[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)

    def align(_path, variants, **kwargs):
        aligned = []
        for line_index, (options, window) in enumerate(
            zip(variants, kwargs["line_windows"], strict=True)
        ):
            moras = options[0]
            for mora_index, mora in enumerate(moras):
                start = window[0] + (window[1] - window[0]) * mora_index / len(moras)
                end = window[0] + (window[1] - window[0]) * (mora_index + 1) / len(moras)
                aligned.append(
                    AlignedMora(line_index, mora_index, mora, start, end, 0.8)
                )
        return aligned, [0] * len(variants)

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    notes = [
        MelodyNote(0.0, 0.8, 60),
        MelodyNote(1.0, 1.8, 62),
        MelodyNote(2.0, 2.8, 64),
        MelodyNote(3.0, 3.8, 65),
        MelodyNote(5.0, 6.0, 67),
    ]
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: notes)
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=6.0)),
    )

    class StopAfterInitialNormalization(Exception):
        pass

    stage3_calls = 0

    def stop_after_initial(
            line_texts, selected_readings, aligned, _melody_notes, **_kwargs,
        ):
        nonlocal stage3_calls
        stage3_calls += 1
        if stage3_calls == 1:
            return SimpleNamespace(
                to_json=lambda: '{"note_candidates": [], "links": []}'
            ), object()
        assert line_texts == ["ラ" * 2, "歌"]
        assert selected_readings == ["ラ" * 2, "ウタ"]
        assert [item.kana for item in aligned if item.line == 0] == ["ラ"] * 2
        raise StopAfterInitialNormalization

    monkeypatch.setattr(stage3, "build_stage3_layers", stop_after_initial)

    with pytest.raises(StopAfterInitialNormalization):
        analyze_audio(
            tmp_path / "input.wav",
            tmp_path / "project",
            device="cpu",
            skip_separation=True,
        )

    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert recognition["semantic_gate"]["localized_deficit_recoveries"] == []
    normalization = recognition["semantic_gate"]["vocalization_normalizations"][0]
    assert normalization["phase"] == "initial"
    assert normalization["original_mora_count"] == 2
    assert normalization["normalized_mora_count"] == 2
    assert normalization["capped"] is False
    assert normalization["expanded"] is False
    assert normalization["adjustment"] == "unchanged"


def test_known_lyrics_audio_path_never_calls_whisper(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora

    def forbidden(*args, **kwargs):
        pytest.fail("known lyrics must not call ASR")

    monkeypatch.setattr(transcribe, "transcribe_lines", forbidden)
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    def align(*args, **kwargs):
        assert kwargs["line_windows"] is None
        return ([AlignedMora(0, 0, "カ", 0.1, 0.12, 0.75),
                 AlignedMora(0, 1, "キ", 0.4, 0.42, 0.65)], [0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.6, 62),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")
    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
        device="cpu", skip_separation=True,
    )
    assert value.lyric_layers["canonical_text"] == "かき"
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert all(note.pitch_confidence is None for note in value.notes)
    assert [x["confidence"] for x in value.lyric_layers["performed"]] == [0.75, 0.65]


def test_known_lyrics_reranks_connected_english_with_fewer_moras(
    monkeypatch, tmp_path,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import analyze_audio as analyze_audio_module
    from soramimic_video import audio_melody, mora_align, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora

    canonical = ["シャ", "ウ", "ト", "イ", "ッ", "ト", "ア", "ウ", "ト"]
    connected = ["シャ", "ウ", "ティ", "タ", "ウ", "ト"]
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=2.0)))
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: ["シャウトイットアウト", "シャウティタウト"],
    )
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    aligned_calls = []

    def align(_path, variants, **kwargs):
        selected = variants[0][0]
        aligned_calls.append(selected)
        return ([
            AlignedMora(0, index, mora, index * 0.2, (index + 1) * 0.2, 0.8)
            for index, mora in enumerate(selected)
        ], [0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    rerank_calls = []

    def rerank(_mix, _vocals, texts, variants, windows, **kwargs):
        rerank_calls.append((texts, variants, windows))
        return [1], {"contexts": [{"start_sec": 0.0, "end_sec": 1.8}]}

    monkeypatch.setattr(analyze_audio_module, "_choose_readings_with_kana", rerank)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(index * 0.2, (index + 1) * 0.2, 60)
        for index in range(len(connected))
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    stage3_calls = []

    class StopAfterReadingSelection(Exception):
        pass

    def stop_after_reading_selection(
        line_texts, selected_readings, aligned, melody_notes, **kwargs,
    ):
        stage3_calls.append((line_texts, selected_readings, [m.kana for m in aligned]))
        raise StopAfterReadingSelection

    monkeypatch.setattr(stage3, "build_stage3_layers", stop_after_reading_selection)
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("Shout it out", encoding="utf-8")

    with pytest.raises(StopAfterReadingSelection):
        analyze_audio(
            tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
            device="cpu", skip_separation=True,
        )

    assert rerank_calls == [
        (["Shout it out"], [[canonical, connected]], [(0.0, 1.8)])
    ]
    assert aligned_calls == [canonical, connected]
    assert stage3_calls == [
        (["Shout it out"], ["シャウティタウト"], connected)
    ]


def test_known_lyrics_ctc_capacity_error_is_not_turned_into_lyric_deletion(
    monkeypatch, tmp_path,
):
    from soramimic_video import audio_melody, mora_align, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.mora_align import CTCWindowCapacityError

    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())

    def reject_capacity(*args, **kwargs):
        assert kwargs["line_windows"] is None
        raise CTCWindowCapacityError(
            available_frames=1,
            target_count=2,
            adjacent_repeats=0,
        )

    monkeypatch.setattr(mora_align, "align_moras_with_variants", reject_capacity)
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": False}
    )
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    with pytest.raises(CTCWindowCapacityError):
        analyze_audio(
            tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
            device="cpu", skip_separation=True,
        )

    assert not (tmp_path / "project/analyze_audio/recognition.json").exists()


def test_audio_path_requires_sheetsage(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.mora_align import AlignedMora

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *args, **kwargs: (
        [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8)], [0],
    ))
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: None)

    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("か", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SheetSage2.*ノート候補"):
        analyze_audio(
            tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
            device="cpu", skip_separation=True,
        )


def test_known_lyrics_audio_path_runs_stage3_for_sheetsage(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *args, **kwargs: (
        [AlignedMora(0, 0, "カ", 0.09, 0.11, 0.1),
         AlignedMora(0, 1, "キ", 0.39, 0.41, 0.1)], [0],
    ))
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
        device="cpu", skip_separation=True,
    )

    assert [(note.kana, note.midi_note) for note in value.notes] == [("カ", 60), ("キ", 62)]
    correspondence = (tmp_path / "project/analyze_audio/correspondence.json").read_text()
    assert '"mora-ctc-anchor"' in correspondence
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["stage3_correspondence"] is True
    assert analysis["schema_version"] == 5
    assert analysis["audio_pipeline"] == "stage3"
    assert analysis["mode"] == "sheetsage2_stage3"
    assert analysis["inference_roles"] == {
        "lyrics": "known-lyrics",
        "mora_timing": "reazon-kana-ctc-input-audio",
        "reading": "yomi-unidic-default-reading",
        "notes": "sheetsage2-original-mix",
        "separation": "skipped-input-as-vocals",
    }
    assert set(analysis["sources"]) == {"sheetsage2-vocal"}
    assert all(note.pitch_confidence is None for note in value.notes)


@pytest.mark.parametrize("known_lyrics", [True, False])
def test_unresolved_stage3_unit_is_omitted_for_known_and_automatic_lyrics(
    monkeypatch, tmp_path, known_lyrics,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *args, **kwargs: [
        TranscribedLine(0.0, 0.5, "かき"),
    ])
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *args, **kwargs: (
        [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8),
         AlignedMora(0, 1, "キ", 0.3, 0.4, 0.7)], [0],
    ))
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.5, 60),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )

    class Document:
        def to_json(self):
            return '{"note_candidates": [], "links": []}'

    class Layers:
        unresolved_unit_ids = ("singing-unit-1",)

        def to_dict(self):
            return {
                "schema_version": 1,
                "canonical_text": "かき",
                "canonical": [{
                    "utterance_id": "utterance-0", "text": "かき", "kana": "カキ",
                    "mora_ids": ["mora-0", "mora-1"],
                }],
                "performed": [
                    {
                        "singing_unit_id": f"singing-unit-{index}",
                        "mora_ids": [f"mora-{index}"], "status": "observed",
                        "start_sec": index * 0.2, "end_sec": (index + 1) * 0.2,
                        "confidence": 0.7, "link_ids": [f"link-{index}"],
                        "evidence_ids": [],
                    }
                    for index in range(2)
                ],
                "synthesis_plan": [{
                    "id": "slot-0", "utterance_id": "utterance-0",
                    "singing_unit_id": "singing-unit-0", "mora_ids": ["mora-0"],
                    "note_candidate_id": "note-0", "link_ids": ["link-0"],
                    "kana": "カ", "start_sec": 0.0, "end_sec": 0.2,
                    "midi_pitch": 60, "operation": "match",
                    "timing_source": "aligned_boundary", "confidence": 0.7,
                    "evidence_ids": [], "pitch_sources": ["sheetsage2-vocal"],
                    "continuation": False,
                }],
                "omissions": [], "unresolved_unit_ids": ["singing-unit-1"],
                "diagnostics": [], "evidence": [],
            }

    monkeypatch.setattr(
        stage3, "build_stage3_layers",
        lambda *args, **kwargs: (Document(), Layers()),
    )
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project",
        lyrics_path=lyrics if known_lyrics else None,
        device="cpu", skip_separation=True,
    )

    assert [note.kana for note in value.notes] == ["カ"]
    assert value.lines[0].original_text == "かき"
    assert value.lyric_layers["unresolved_unit_ids"] == []
    assert value.lyric_layers["omissions"][0]["singing_unit_id"] == "singing-unit-1"
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["stage3_correspondence"] is True
    assert analysis["lyric_asr_used"] is not known_lyrics
    assert analysis["diagnostics"][-1]["status"] == "synthesis-omission"


def test_known_lyrics_fails_truthfully_when_stage3_plan_is_invalid(monkeypatch, tmp_path):
    import soramimic_video.stage3 as stage3
    from soramimic_video import audio_melody, mora_align, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *args, **kwargs: (
        [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8),
         AlignedMora(0, 1, "キ", 0.3, 0.4, 0.7)], [0],
    ))
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.5, 60),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )

    def invalid_plan(*args, **kwargs):
        raise ValueError("invalid synthesis slot timing, pitch, or confidence")

    monkeypatch.setattr(stage3, "build_stage3_layers", invalid_plan)
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    with pytest.raises(RuntimeError, match="歌詞や音高を補わず"):
        analyze_audio(
            tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
            device="cpu", skip_separation=True,
        )

    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["stage3_correspondence"] is False
    assert analysis["diagnostics"][-1]["status"] == "unresolved"
    assert "補わず" in analysis["limitations"][-1]
    assert not (tmp_path / "project/analyze_audio/correspondence.json").exists()


def test_partial_recognition_windows_survive_alignment(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, kana_whisper, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    emissions = object()
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=10.0)),
    )

    def recognize(path, model, device, *, vad_filter, condition_on_previous_text):
        calls.append((path, model, device, vad_filter, condition_on_previous_text))
        return [TranscribedLine(1., 2., "か"), TranscribedLine(8., 9., "き")]

    monkeypatch.setattr(transcribe, "transcribe_lines", recognize)
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: [{"か": "カ", "き": "キ"}[text], {"か": "キ", "き": "ク"}[text]],
    )
    monkeypatch.setattr(
        kana_whisper,
        "transcribe_kana_windows",
        lambda path, windows, device: ["カキ" for _window in windows],
    )
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)

    def align(path, variants, **kwargs):
        assert variants == [[["カ"]], [["キ"]]]
        assert kwargs["line_windows"] == [(1., 2.), (8., 9.)]
        assert kwargs["emissions"] is emissions
        return ([AlignedMora(0, 0, "カ", 1.2, 1.3, .75),
                 AlignedMora(1, 0, "キ", 8.2, 8.3, .65)], [0, 0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1.0, 2.0, 60), MelodyNote(8.0, 9.0, 62),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )
    assert calls == [(tmp_path / "input.wav", "large-v3", "cpu", False, False)]
    assert [n.end_sec for n in value.notes] == [2., 9.]
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert value.lyric_layers["canonical_text"] == "か\nき"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert recognition["schema_version"] == 6
    assert recognition["mode"] == "whisper-mix-semantic-gate"
    assert recognition["transcription_options"] == {
        "vad_filter": False,
        "condition_on_previous_text": False,
    }
    assert recognition["semantic_gate"]["vocal_activity"] == {
        "applied": False,
        "reason": "separation-skipped",
    }
    assert [item["status"] for item in recognition["semantic_gate"]["decisions"]] == [
        "accepted", "accepted",
    ]
    assert [item["surface"] for item in recognition["segments"]] == ["か", "き"]
    reading_evidence = json.loads(
        (tmp_path / "project/analyze_audio/reading.json").read_text()
    )
    assert reading_evidence["schema_version"] == 4
    assert reading_evidence["mode"] == "closed-reading-candidate-rerank"
    assert reading_evidence["distance_metric"] == "kanasim-weighted-substring-0.0.11"
    assert [line["selected_index"] for line in reading_evidence["lines"]] == [0, 0]


def test_infeasible_automatic_ctc_line_is_rejected_whole(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora, CTCWindowCapacityError
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *args, **kwargs: [
        TranscribedLine(1.0, 2.0, "かき"),
        TranscribedLine(8.0, 9.0, "く"),
    ])
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: {"かき": ["カキ"], "く": ["ク"]}[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args, **kwargs: emissions)
    calls = []

    def align(path, variants, **kwargs):
        calls.append((variants, kwargs["line_windows"]))
        assert kwargs["emissions"] is emissions
        if len(variants) == 2:
            raise CTCWindowCapacityError(
                line=0,
                available_frames=52,
                target_count=57,
                adjacent_repeats=0,
            )
        assert variants == [[["ク"]]]
        return [AlignedMora(0, 0, "ク", 8.2, 8.3, 0.8)], [0]

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(1.0, 2.0, 60), MelodyNote(8.0, 9.0, 62),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=10.0)),
    )

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )

    assert calls == [
        ([[["カ", "キ"]], [["ク"]]], [(1.0, 2.0), (8.0, 9.0)]),
        ([[["ク"]]], [(8.0, 9.0)]),
    ]
    assert value.lyric_layers["canonical_text"] == "く"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert [item["surface"] for item in recognition["segments"]] == ["く"]
    assert [item["status"] for item in recognition["semantic_gate"]["decisions"]] == [
        "rejected", "accepted",
    ]
    assert recognition["semantic_gate"]["ctc_capacity_rejections"] == [{
        "phase": "initial",
        "source_segment_index": 0,
        "source_retained_index": 0,
        "start_sec": 1.0,
        "end_sec": 2.0,
        "surface": "かき",
        "status": "rejected",
        "reason": "ctc-window-capacity-insufficient",
        "available_frames": 52,
        "required_frames": 57,
        "target_count": 57,
        "adjacent_repeats": 0,
    }]
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text()
    )
    assert analysis["ctc_capacity_rejections"] == (
        recognition["semantic_gate"]["ctc_capacity_rejections"]
    )
    assert any(
        item["stage"] == "mora-ctc-capacity"
        and item["reason"] == "ctc-window-capacity-insufficient"
        for item in analysis["diagnostics"]
    )


def test_overlapping_recognition_windows_retry_global_ctc_without_dropping_lyrics(
    monkeypatch, tmp_path, caplog,
):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: [
        TranscribedLine(1., 3., "か"), TranscribedLine(2., 4., "き")])
    monkeypatch.setattr(reading, "reading_candidates",
                        lambda text: [{"か": "カ", "き": "キ"}[text]])
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)
    calls = []

    def align(path, variants, **kwargs):
        calls.append(kwargs["line_windows"])
        assert kwargs["emissions"] is emissions
        if kwargs["line_windows"] == [(1., 3.), (2., 4.)]:
            raise ValueError("line_windows must be finite, positive, and nonoverlapping")
        if kwargs["line_windows"] == [(2., 4.)]:
            return ([AlignedMora(0, 0, "キ", 3.2, 3.3, .9)], [0])
        return ([AlignedMora(0, 0, "カ", 1.2, 1.3, .8),
                 AlignedMora(1, 0, "キ", 3.2, 4.9, .7)], [0, 0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1., 2., 60), MelodyNote(3., 4., 62)])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=5.)))

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )

    assert calls == [[(1., 3.), (2., 4.)], None, [(2., 4.)]]
    assert [note.kana for note in value.notes] == ["カ", "キ"]
    assert value.lyric_layers["canonical_text"] == "か\nき"
    assert "全体整列へ切替" in caplog.text
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert any("CTC全体整列" in item for item in analysis["limitations"])
    assert analysis["localized_alignment_retries"] == [{
        "line": 1,
        "window_start_sec": 2.0,
        "window_end_sec": 4.0,
        "reasons": ["after-whisper-window"],
        "before_max_mora_span_sec": pytest.approx(1.7),
        "before_line_end_sec": 4.9,
        "after_max_mora_span_sec": pytest.approx(.1),
        "after_line_end_sec": 3.3,
        "status": "replaced",
    }]


def test_semantic_gate_discards_only_no_melody_template_segment(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: [
        TranscribedLine(1., 2., "歌"),
        TranscribedLine(8., 9., "ご視聴ありがとうございました"),
    ])
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["ウタ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *a, **kw: (
        [AlignedMora(0, 0, "ウ", 1.2, 1.3, .8),
         AlignedMora(0, 1, "タ", 1.4, 1.5, .8)], [0]))
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1., 2., 60)])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )

    assert value.lyric_layers["canonical_text"] == "歌"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text())
    assert [item["surface"] for item in recognition["segments"]] == ["歌"]
    decisions = recognition["semantic_gate"]["decisions"]
    assert [(item["surface"], item["status"]) for item in decisions] == [
        ("歌", "accepted"),
        ("ご視聴ありがとうございました", "rejected"),
    ]
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text()
    )
    assert analysis["diagnostics"][0]["status"] == "rejected"


def test_semantic_gate_realigns_after_ctc_rejects_melodic_template(
    monkeypatch, tmp_path,
):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: [
        TranscribedLine(1., 2., "作曲"),
        TranscribedLine(8., 9., "歌"),
    ])
    monkeypatch.setattr(
        reading, "reading_candidates",
        lambda text: {"作曲": ["サッキョク"], "歌": ["ウタ"]}[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)
    calls = []

    def align(path, variants, **kwargs):
        calls.append((variants, kwargs["line_windows"]))
        assert kwargs["emissions"] is emissions
        if len(variants) == 2:
            return ([
                AlignedMora(0, 0, "サ", 1.1, 1.2, .0004),
                AlignedMora(0, 1, "ッ", 1.2, 1.3, .0006),
                AlignedMora(0, 2, "キョ", 1.3, 1.4, .0008),
                AlignedMora(0, 3, "ク", 1.4, 1.5, .0005),
                AlignedMora(1, 0, "ウ", 8.2, 8.3, .8),
                AlignedMora(1, 1, "タ", 8.4, 8.5, .7),
            ], [0, 0])
        assert variants == [[["ウ", "タ"]]]
        return ([
            AlignedMora(0, 0, "ウ", 8.2, 8.3, .8),
            AlignedMora(0, 1, "タ", 8.4, 8.5, .7),
        ], [0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1., 2., 60), MelodyNote(8., 9., 62),
    ])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )

    assert [windows for _variants, windows in calls] == [
        [(1., 2.), (8., 9.)], [(8., 9.)],
    ]
    assert value.lyric_layers["canonical_text"] == "歌"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert [item["surface"] for item in recognition["segments"]] == ["歌"]
    decision = recognition["semantic_gate"]["decisions"][0]
    assert decision["status"] == "rejected"
    assert decision["melodic_support"] is True
    assert decision["ctc_support"] is False
    assert decision["ctc_median_score"] == pytest.approx(.00055)


def test_semantic_gate_recovers_singing_island_before_final_ctc(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: [
        TranscribedLine(0., 5., "作曲"), TranscribedLine(8., 9., "歌")])
    recovered_calls = []

    def recover(path, start, end, model, device):
        recovered_calls.append((start, end, model, device))
        return [TranscribedLine(start, end, "空")]

    monkeypatch.setattr(transcribe, "transcribe_window", recover)
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: {"作曲": ["サッキョク"], "空": ["ソラ"], "歌": ["ウタ"]}[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)
    calls = []

    def align(path, variants, **kwargs):
        calls.append(kwargs["line_windows"])
        if len(variants[0][0]) == 4:
            return ([
                # Even strong coincidental CTC support cannot validate an exact
                # credit template; the bounded Whisper recovery must still run.
                AlignedMora(0, 0, "サ", 1.1, 1.2, .8),
                AlignedMora(0, 1, "ッ", 1.2, 1.3, .8),
                AlignedMora(0, 2, "キョ", 1.3, 1.4, .8),
                AlignedMora(0, 3, "ク", 1.4, 1.5, .8),
                AlignedMora(1, 0, "ウ", 8.2, 8.3, .8),
                AlignedMora(1, 1, "タ", 8.4, 8.5, .7),
            ], [0, 0])
        return ([
            AlignedMora(0, 0, "ソ", 1.2, 1.3, .8),
            AlignedMora(0, 1, "ラ", 3.5, 3.7, .8),
            AlignedMora(1, 0, "ウ", 8.2, 8.3, .8),
            AlignedMora(1, 1, "タ", 8.4, 8.5, .7),
        ], [0, 0])

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1., 4., 60), MelodyNote(8., 9., 62)])
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", device="cpu",
        skip_separation=True,
    )

    assert recovered_calls == [(1., 4., "large-v3", "cpu")]
    assert calls == [[(0., 5.), (8., 9.)], [(1., 4.), (8., 9.)]]
    assert value.lyric_layers["canonical_text"] == "空\n歌"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text())
    recovery = recognition["semantic_gate"]["localized_recoveries"]
    assert recovery[0]["status"] == "accepted"
    decision = recognition["semantic_gate"]["decisions"][0]
    assert decision["ctc_median_score"] == pytest.approx(.8)
    assert decision["ctc_support"] is False
    assert [item["surface"] for item in recognition["segments"]] == ["空", "歌"]
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert not any("CTC全体整列" in item for item in analysis["limitations"])


@pytest.mark.parametrize(
    ("retry_count", "expected_status"),
    [(2, "accepted"), (100, "unresolved")],
)
def test_credit_retry_preserves_observed_repetition_attacks(
    monkeypatch, tmp_path, retry_count, expected_status,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        audio_melody,
        mora_align,
        reading,
        transcribe,
    )
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(
        transcribe,
        "transcribe_lines",
        lambda *a, **kw: [
            TranscribedLine(0.0, 5.0, "作曲"),
            TranscribedLine(8.0, 9.0, "歌"),
        ],
    )
    monkeypatch.setattr(
        transcribe,
        "transcribe_window",
        lambda _path, start, end, _model, _device: [
            TranscribedLine(start, end, "ダ" * retry_count)
        ],
    )
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: {
            "作曲": ["サッキョク"],
            "歌": ["ウタ"],
            "ダ" * retry_count: ["ダ" * retry_count],
        }[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)
    monkeypatch.setattr(
        analyze_audio_module,
        "_choose_readings_with_kana",
        lambda _mix, _vocals, _texts, variants, _windows, **_kwargs: (
            [0] * len(variants),
            None,
        ),
    )

    def align(_path, variants, **kwargs):
        aligned = []
        for line_index, (options, window) in enumerate(
            zip(variants, kwargs["line_windows"], strict=True)
        ):
            moras = options[0]
            for mora_index, mora in enumerate(moras):
                start = window[0] + (window[1] - window[0]) * mora_index / len(moras)
                end = window[0] + (window[1] - window[0]) * (mora_index + 1) / len(moras)
                aligned.append(
                    AlignedMora(line_index, mora_index, mora, start, end, 0.8)
                )
        return aligned, [0] * len(variants)

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    notes = [
        MelodyNote(1.0, 1.4, 60),
        MelodyNote(1.8, 2.2, 62),
        MelodyNote(2.6, 3.0, 64),
        MelodyNote(3.4, 3.8, 65),
        MelodyNote(8.0, 9.0, 67),
    ]
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: notes)
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=10.0)),
    )

    class StopAfterRecovery(Exception):
        pass

    stage3_calls = 0

    def stop_after_recovery(
            line_texts, selected_readings, aligned, _melody_notes, **_kwargs,
        ):
        nonlocal stage3_calls
        stage3_calls += 1
        if stage3_calls == 1:
            return SimpleNamespace(
                to_json=lambda: '{"note_candidates": [], "links": []}'
            ), object()
        if expected_status == "accepted":
            assert line_texts == ["ダ" * 2, "歌"]
            assert selected_readings == ["ダ" * 2, "ウタ"]
            assert [item.kana for item in aligned if item.line == 0] == ["ダ"] * 2
        else:
            assert line_texts == ["歌"]
            assert selected_readings == ["ウタ"]
            assert [item.kana for item in aligned if item.line == 0] == ["ウ", "タ"]
        raise StopAfterRecovery

    monkeypatch.setattr(stage3, "build_stage3_layers", stop_after_recovery)

    with pytest.raises(StopAfterRecovery):
        analyze_audio(
            tmp_path / "input.wav",
            tmp_path / "project",
            device="cpu",
            skip_separation=True,
        )

    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    recovery = recognition["semantic_gate"]["localized_recoveries"][0]
    assert recovery["status"] == expected_status
    if expected_status == "accepted":
        assert recovery["rejection_reasons"] == []
        assert recovery["segments"][0]["surface"] == "ダ" * 2
        normalization = recognition["semantic_gate"]["vocalization_normalizations"][0]
        assert normalization["phase"] == "semantic-recovery"
        assert normalization["original_mora_count"] == 2
        assert normalization["normalized_mora_count"] == 2
        assert normalization["note_count"] == 4
        assert normalization["capped"] is False
        assert normalization["expanded"] is False
        assert normalization["adjustment"] == "unchanged"
    else:
        assert recovery["rejection_reasons"] == ["pathological-repetition"]
        assert recovery["segments"] == []
        assert recognition["semantic_gate"]["vocalization_normalizations"] == []


def test_note_lyric_deficit_recovery_replaces_only_acoustically_supported_detail(
    monkeypatch, tmp_path,
):
    from soramimic_video import audio_melody, mora_align, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    original = [
        TranscribedLine(0.0, 2.0, "かきくけ"),
        TranscribedLine(3.0, 5.0, "こさしす"),
        TranscribedLine(6.0, 8.0, "せそたち"),
        TranscribedLine(10.0, 13.0, "つてとな"),
    ]
    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: original)
    retry_calls = []

    def recover(path, start, end, model, device):
        retry_calls.append((start, end))
        text = "にぬねの" if start < 11.0 else "はひふへ"
        return [TranscribedLine(start, end, text)]

    monkeypatch.setattr(transcribe, "transcribe_window", recover)
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: [{
            "かきくけ": "カキクケ",
            "こさしす": "コサシス",
            "せそたち": "セソタチ",
            "つてとな": "ツテトナ",
            "にぬねの": "ニヌネノ",
            "はひふへ": "ハヒフヘ",
        }[text]],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)

    def align(path, variants, **kwargs):
        windows = kwargs["line_windows"]
        assert windows is not None
        aligned = []
        for line_index, (options, window) in enumerate(zip(variants, windows, strict=True)):
            moras = options[0]
            for mora_index, mora in enumerate(moras):
                start = window[0] + (window[1] - window[0]) * mora_index / len(moras)
                end = window[0] + (window[1] - window[0]) * (mora_index + 1) / len(moras)
                aligned.append(
                    AlignedMora(line_index, mora_index, mora, start, end, 0.8)
                )
        return aligned, [0] * len(variants)

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    notes = []
    for base in (0.0, 3.0, 6.0):
        notes.extend(
            MelodyNote(base + index * 0.2, base + index * 0.2 + 0.1, 60)
            for index in range(4)
        )
    notes.extend(
        MelodyNote(10.0 + index * 0.2, 10.1 + index * 0.2, 62)
        for index in range(5)
    )
    notes.extend(
        MelodyNote(11.4 + index * 0.2, 11.5 + index * 0.2, 64)
        for index in range(5)
    )
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: notes)
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=13.0)),
    )

    value = analyze_audio(
        tmp_path / "input.wav",
        tmp_path / "project",
        device="cpu",
        skip_separation=True,
    )

    assert retry_calls[0] == pytest.approx((10.0, 11.15))
    assert retry_calls[1] == pytest.approx((11.15, 12.8))
    assert value.lyric_layers["canonical_text"].splitlines()[-2:] == [
        "にぬねの", "はひふへ",
    ]
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    recovery = recognition["semantic_gate"]["localized_deficit_recoveries"]
    assert len(recovery) == 1
    assert recovery[0]["status"] == "accepted"
    assert recovery[0]["source_surface"] == "つてとな"
    assert recovery[0]["source_mora_count"] == 4
    assert recovery[0]["recovered_mora_count"] == 8
    assert [item["surface"] for item in recognition["segments"][-2:]] == [
        "にぬねの", "はひふへ",
    ]


@pytest.mark.parametrize(
    ("retry_surface", "kana_surface", "expected_status", "expected_count"),
    [
        ("DADADADA", "ダ" * 4, "accepted", 4),
        ("DADADADA", "ダ" * 12, "accepted", 12),
        ("DA" * 100, None, "rejected", 100),
    ],
)
def test_note_deficit_retry_handles_pure_repeated_vocalization(
    monkeypatch, tmp_path, retry_surface, kana_surface, expected_status,
    expected_count,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        audio_melody,
        kana_whisper,
        mora_align,
        reading,
        transcribe,
    )
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    original = [
        TranscribedLine(0.0, 2.0, "かきくけ"),
        TranscribedLine(3.0, 5.0, "こさしす"),
        TranscribedLine(6.0, 8.0, "せそたち"),
        TranscribedLine(10.0, 13.0, "短い"),
    ]
    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: original)
    monkeypatch.setattr(
        transcribe,
        "transcribe_window",
        lambda _path, start, end, _model, _device: [
            TranscribedLine(start, end, retry_surface)
        ],
    )
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: [
            text
            if text and set(text) == {"ダ"}
            else {
                "かきくけ": "カキクケ",
                "こさしす": "コサシス",
                "せそたち": "セソタチ",
                "短い": "ミジカイ",
            }[text]
        ],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)
    monkeypatch.setattr(
        analyze_audio_module,
        "_choose_readings_with_kana",
        lambda _mix, _vocals, _texts, variants, _windows, **_kwargs: (
            [0] * len(variants),
            None,
        ),
    )

    def align(_path, variants, **kwargs):
        aligned = []
        for line_index, (options, window) in enumerate(
            zip(variants, kwargs["line_windows"], strict=True)
        ):
            moras = options[0]
            for mora_index, mora in enumerate(moras):
                start = window[0] + (window[1] - window[0]) * mora_index / len(moras)
                end = window[0] + (window[1] - window[0]) * (mora_index + 1) / len(moras)
                aligned.append(
                    AlignedMora(line_index, mora_index, mora, start, end, 0.01)
                )
        return aligned, [0] * len(variants)

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(
        kana_whisper,
        "transcribe_kana_windows",
        lambda _path, windows, _device: [kana_surface for _window in windows]
        if kana_surface is not None
        else pytest.fail("pathological retry must not invoke KanaWhisper"),
    )
    notes = []
    for base in (0.0, 3.0, 6.0):
        notes.extend(
            MelodyNote(base + index * 0.2, base + index * 0.2 + 0.1, 60)
            for index in range(4)
        )
    notes.extend(
        MelodyNote(10.0 + index * 0.15, 10.1 + index * 0.15, 62)
        for index in range(16)
    )
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: notes)
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda path: SimpleNamespace(duration=13.0)),
    )

    class StopAfterRecovery(Exception):
        pass

    stage3_calls = 0

    def stop_after_recovery(
            line_texts, selected_readings, aligned, _melody_notes, **_kwargs,
        ):
        nonlocal stage3_calls
        stage3_calls += 1
        if stage3_calls == 1:
            return SimpleNamespace(
                to_json=lambda: '{"note_candidates": [], "links": []}'
            ), object()
        expected_surface = (
            "ダ" * expected_count if expected_status == "accepted" else "短い"
        )
        expected_reading = (
            "ダ" * expected_count if expected_status == "accepted" else "ミジカイ"
        )
        assert line_texts[-1] == expected_surface
        assert selected_readings[-1] == expected_reading
        expected_moras = (
            ["ダ"] * expected_count
            if expected_status == "accepted"
            else list("ミジカイ")
        )
        assert [item.kana for item in aligned if item.line == 3] == expected_moras
        raise StopAfterRecovery

    monkeypatch.setattr(stage3, "build_stage3_layers", stop_after_recovery)

    with pytest.raises(StopAfterRecovery):
        analyze_audio(
            tmp_path / "input.wav",
            tmp_path / "project",
            device="cpu",
            skip_separation=True,
        )

    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    recovery = recognition["semantic_gate"]["localized_deficit_recoveries"][0]
    assert recovery["status"] == expected_status
    assert recovery["classification"] == "repeated-vocalization"
    assert recovery["rejection_reasons"] == (
        [] if expected_status == "accepted" else ["pathological-repetition"]
    )
    assert recovery["recovered_mora_count"] == expected_count
    assert recovery["segments"][0]["surface"] == "ダ" * expected_count
    assert recognition["segments"][-1]["surface"] == (
        "ダ" * expected_count if expected_status == "accepted" else "短い"
    )
    assert recovery["selected_kana_whisper_source"] == (
        "original-mix" if expected_count == 12 else None
    )
    normalization = recognition["semantic_gate"]["vocalization_normalizations"][0]
    assert normalization["phase"] == "deficit-recovery"
    assert normalization["original_surface"] == retry_surface
    retry_count = 100 if retry_surface == "DA" * 100 else 4
    assert normalization["surface"] == "ダ" * retry_count
    assert normalization["original_mora_count"] == retry_count
    assert normalization["normalized_mora_count"] == retry_count
    assert normalization["note_count"] == 16
    assert normalization["capped"] is False
    assert normalization["expanded"] is False
    assert normalization["adjustment"] == "unchanged"
