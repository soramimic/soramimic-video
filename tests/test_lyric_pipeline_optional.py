"""Integration contract tests, run when the optional local pipeline is installed."""

import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("wav_to_xf.pipeline")


def test_stage3_uses_note_run_config_with_each_mora_ctc_peak(
    monkeypatch,
):
    from wav_to_xf import pipeline

    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    captured = {}
    original = pipeline.run_stage3_document

    def run(document, *, config=None, **kwargs):
        captured["config"] = config
        return original(document, config=config, **kwargs)

    monkeypatch.setattr(pipeline, "run_stage3_document", run)

    document, realization = build_stage3_layers(
        ["かき"], ["カキ"],
        [AlignedMora(0, 0, "カ", 0.09, 0.11, 0.1),
         AlignedMora(0, 1, "キ", 0.39, 0.41, 0.1)],
        [MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62)],
    )

    anchors = [item for item in document.evidence if item.kind == "mora-ctc-anchor"]
    assert [item.detail["time_sec"] for item in anchors] == pytest.approx([0.1, 0.4])
    from wav_to_xf import NoteRunConfig

    assert captured["config"] == NoteRunConfig()
    notes = {item.id: item for item in document.note_candidates}
    assert [
        (item.kana, notes[item.note_candidate_id].midi_pitch)
        for item in realization.synthesis_plan
    ] == [
        ("カ", 60), ("キ", 62),
    ]
    assert not realization.unresolved_unit_ids


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
    assert analysis["schema_version"] == 4
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
            return "{}"

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

    monkeypatch.setattr(stage3, "build_stage3_layers", lambda *args: (Document(), Layers()))
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

    def invalid_plan(*args):
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
        lambda text: [{"か": "カ", "き": "キ"}[text], "サ"],
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
    assert recognition["schema_version"] == 4
    assert recognition["mode"] == "whisper-mix-semantic-gate"
    assert recognition["transcription_options"] == {
        "vad_filter": False,
        "condition_on_previous_text": False,
    }
    assert [item["status"] for item in recognition["semantic_gate"]["decisions"]] == [
        "accepted", "accepted",
    ]
    assert [item["surface"] for item in recognition["segments"]] == ["か", "き"]
    reading_evidence = json.loads(
        (tmp_path / "project/analyze_audio/reading.json").read_text()
    )
    assert reading_evidence["schema_version"] == 3
    assert reading_evidence["mode"] == "closed-reading-candidate-rerank"
    assert reading_evidence["distance_metric"] == "kanasim-weighted-substring-0.0.11"
    assert [line["selected_index"] for line in reading_evidence["lines"]] == [0, 0]


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
        if kwargs["line_windows"] is not None:
            raise ValueError("line_windows must be finite, positive, and nonoverlapping")
        return ([AlignedMora(0, 0, "カ", 1.2, 1.3, .8),
                 AlignedMora(1, 0, "キ", 3.2, 3.3, .7)], [0, 0])

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

    assert calls == [[(1., 3.), (2., 4.)], None]
    assert [note.kana for note in value.notes] == ["カ", "キ"]
    assert value.lyric_layers["canonical_text"] == "か\nき"
    assert "全体整列へ切替" in caplog.text
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert any("CTC全体整列" in item for item in analysis["limitations"])


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
                AlignedMora(0, 0, "サ", 1.1, 1.2, .0004),
                AlignedMora(0, 1, "ッ", 1.2, 1.3, .0005),
                AlignedMora(0, 2, "キョ", 1.3, 1.4, .0006),
                AlignedMora(0, 3, "ク", 1.4, 1.5, .0005),
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
    assert [item["surface"] for item in recognition["segments"]] == ["空", "歌"]
    analysis = json.loads(
        (tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert not any("CTC全体整列" in item for item in analysis["limitations"])
