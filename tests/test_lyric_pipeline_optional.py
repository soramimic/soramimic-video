"""Integration contract tests, run when the optional local pipeline is installed."""

import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("wav_to_xf.cplus")

from soramimic_video.lyric_layers import apply_lyric_layers  # noqa: E402
from soramimic_video.project import Project, SongInfo  # noqa: E402


def test_stage3_uses_boundaryless_weights_with_each_mora_ctc_peak(
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
    assert captured["config"].vowel_onset_weight == 0
    assert captured["config"].interval_overlap_weight == 0
    assert captured["config"].boundary_weight == 0
    assert [(item.kana, item.note_candidate_id) for item in realization.synthesis_plan] == [
        ("カ", "sheetsage-0"), ("キ", "sheetsage-1"),
    ]
    assert not realization.unresolved_unit_ids


def test_cplus_runtime_rows_roundtrip_into_project():
    from wav_to_xf.cplus import from_cplus_assignments

    rows = [{"line": 0, "kana": kana, "start_sec": i * 0.3, "end_sec": (i + 1) * 0.3,
             "alignment_start_sec": i * 0.3, "alignment_end_sec": i * 0.3 + 0.02,
             "alignment_score": score, "midi_note": 60, "source": "sheetsage_note",
             "pitch_confidence": None}
            for i, (kana, score) in enumerate(zip("カカカ", [0.8, 0, 0.7], strict=True))]
    ir, realization = from_cplus_assignments(["かかか"], ["カカカ"], rows)
    assert [x.status for x in ir.singing_units] == ["observed", "unobserved", "observed"]
    assert all(x.start is None for x in ir.vowel_nuclei)
    value = Project(SongInfo("", 480, tempo_map=[[0, 500000]]))
    apply_lyric_layers(value, realization.to_dict())
    assert [n.kana for n in value.notes] == list("カカカ")
    assert all(n.pitch_confidence is None for n in value.notes)


def test_known_lyrics_evidence_path_never_calls_whisper(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, pitch, reading, transcribe
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
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: start + 0.2)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60, 62])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.6, 62),
    ])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": True, "rmvpe": False, "fcpe": False,
    })
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")
    value = analyze_audio(tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
                          device="cpu", skip_separation=True, lyric_pipeline="evidence")
    assert value.lyric_layers["canonical_text"] == "かき"
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert [x["confidence"] for x in value.lyric_layers["performed"]] == [0.75, 0.65]


def test_evidence_path_requires_sheetsage(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, pitch, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.mora_align import AlignedMora

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=1.0)))
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *args: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *args, **kwargs: (
        [AlignedMora(0, 0, "カ", 0.1, 0.2, 0.8)], [0],
    ))
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: start + 0.1)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: None)

    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("か", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SheetSage2モデル設定"):
        analyze_audio(
            tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
            device="cpu", skip_separation=True, lyric_pipeline="evidence",
        )


def test_known_lyrics_evidence_path_runs_stage3_for_sheetsage(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, pitch, reading
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
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: start + 0.1)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60, 62])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.3, 60), MelodyNote(0.3, 0.5, 62),
    ])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": True, "rmvpe": False, "fcpe": False,
    })
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
        device="cpu", skip_separation=True, lyric_pipeline="evidence",
    )

    assert [(note.kana, note.midi_note) for note in value.notes] == [("カ", 60), ("キ", 62)]
    correspondence = (tmp_path / "project/analyze_audio/correspondence.json").read_text()
    assert '"mora-ctc-anchor"' in correspondence
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["stage3_correspondence"] is True


@pytest.mark.parametrize("failure", ["unresolved", "invalid-plan"])
def test_known_lyrics_falls_back_when_stage3_cannot_supply_a_complete_plan(
    monkeypatch, tmp_path, failure,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import audio_melody, mora_align, pitch, reading
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
    monkeypatch.setattr(pitch, "extract_pitch", lambda *args: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: start + 0.1)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *args: [60, 62])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: [
        MelodyNote(0.0, 0.5, 60),
    ])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": True, "rmvpe": False, "fcpe": False,
    })

    class Document:
        def to_json(self):
            return "{}"

    class Layers:
        unresolved_unit_ids = ("singing-unit-1",)

        def to_dict(self):
            pytest.fail("unresolved Stage 3 layers must not replace the complete project")

    if failure == "unresolved":
        monkeypatch.setattr(
            stage3, "build_stage3_layers", lambda *args: (Document(), Layers())
        )
    else:
        def invalid_plan(*args):
            raise ValueError("invalid synthesis slot timing, pitch, or confidence")

        monkeypatch.setattr(stage3, "build_stage3_layers", invalid_plan)
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")

    value = analyze_audio(
        tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
        device="cpu", skip_separation=True, lyric_pipeline="evidence",
    )

    assert value.lyric_layers is None
    assert [note.kana for note in value.notes] == ["カ", "キ"]
    assert [note.midi_note for note in value.notes] == [60, 60]
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["stage3_correspondence"] is False
    assert "CTC整列結果" in analysis["limitations"][-1]
    if failure == "invalid-plan":
        assert not (tmp_path / "project/analyze_audio/correspondence.json").exists()


def test_partial_recognition_windows_survive_alignment_and_voiced_extension(monkeypatch, tmp_path):
    from soramimic_video import audio_activity, audio_melody, mora_align, pitch, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    emissions = object()
    calls = []

    def recognize(path, model, device, *, vad_filter, condition_on_previous_text):
        calls.append((path, model, device, vad_filter, condition_on_previous_text))
        return [TranscribedLine(1., 2., "か"), TranscribedLine(8., 9., "き")]

    monkeypatch.setattr(transcribe, "transcribe_lines", recognize)
    monkeypatch.setattr(
        audio_activity,
        "detect_audio_activity",
        lambda path: [audio_activity.ActivityInterval(1., 2.),
                      audio_activity.ActivityInterval(8., 9.)],
    )
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: [{"か": "カ", "き": "キ"}[text], "サ"],
    )
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: emissions)

    def align(path, variants, **kwargs):
        assert variants == [[["カ"]], [["キ"]]]
        assert kwargs["line_windows"] == [(1., 2.), (8., 9.)]
        assert kwargs["emissions"] is emissions
        return ([AlignedMora(0, 0, "カ", 1.2, 1.3, .75),
                 AlignedMora(1, 0, "キ", 8.2, 8.3, .65)], [0, 0])

    extensions = []

    def extend(track, start, limit):
        extensions.append((start, limit))
        return limit + .25

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    monkeypatch.setattr(pitch, "extract_pitch", lambda *a: None)
    monkeypatch.setattr(pitch, "voiced_end", extend)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *a: [60, 62])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1.0, 2.0, 60), MelodyNote(8.0, 9.0, 62),
    ])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": True, "rmvpe": False, "fcpe": False,
    })
    value = analyze_audio(tmp_path / "input.wav", tmp_path / "project", device="cpu",
                          skip_separation=True, lyric_pipeline="evidence")
    assert calls == [(tmp_path / "input.wav", "large-v3", "cpu", False, False)]
    assert extensions == [(1.2, 2.), (8.2, 9.)]
    assert [n.end_sec for n in value.notes] == [2., 9.]
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert value.lyric_layers["canonical_text"] == "か\nき"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    assert recognition["schema_version"] == 2
    assert recognition["mode"] == "whisper-mix-silence-guard"
    assert recognition["transcription_options"] == {
        "vad_filter": False,
        "condition_on_previous_text": False,
    }
    assert recognition["silence_guard"]["discarded_segments"] == []
    assert [item["surface"] for item in recognition["segments"]] == ["か", "き"]


def test_silence_guard_discards_only_fully_inactive_whisper_segment(monkeypatch, tmp_path):
    from soramimic_video import audio_activity, audio_melody, mora_align, pitch, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine

    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *a, **kw: [
        TranscribedLine(1., 2., "歌"), TranscribedLine(8., 9., "幻覚")])
    monkeypatch.setattr(audio_activity, "detect_audio_activity", lambda path: [
        audio_activity.ActivityInterval(1.5, 1.7)])
    monkeypatch.setattr(reading, "reading_candidates", lambda text: ["ウタ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *a, **kw: object())
    monkeypatch.setattr(mora_align, "align_moras_with_variants", lambda *a, **kw: (
        [AlignedMora(0, 0, "ウ", 1.2, 1.3, .8),
         AlignedMora(0, 1, "タ", 1.4, 1.5, .8)], [0]))
    monkeypatch.setattr(pitch, "extract_pitch", lambda *a: None)
    monkeypatch.setattr(pitch, "voiced_end", lambda track, start, limit: limit)
    monkeypatch.setattr(pitch, "mora_midi_notes", lambda *a: [60, 60])
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: [
        MelodyNote(1., 2., 60)])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": True, "rmvpe": False, "fcpe": False})
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))

    value = analyze_audio(tmp_path / "input.wav", tmp_path / "project", device="cpu",
                          skip_separation=True, lyric_pipeline="evidence")

    assert value.lyric_layers["canonical_text"] == "歌"
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text())
    assert [item["surface"] for item in recognition["segments"]] == ["歌"]
    assert [item["surface"] for item in
            recognition["silence_guard"]["discarded_segments"]] == ["幻覚"]
