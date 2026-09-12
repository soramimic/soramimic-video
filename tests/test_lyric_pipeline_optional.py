"""Integration contract tests, run when the optional local pipeline is installed."""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("wav_to_xf.cplus")

from soramimic_video import lyric_recognition  # noqa: E402
from soramimic_video.lyric_layers import apply_lyric_layers  # noqa: E402
from soramimic_video.mora_align import CTCEmissions  # noqa: E402
from soramimic_video.project import Project, SongInfo  # noqa: E402


def test_multiview_runtime_reuses_models_and_retains_real_scores(monkeypatch):
    calls = []
    models = []

    class Whisper:
        def __init__(self, *args, **kwargs):
            models.append(self)

        def transcribe(self, path, **kwargs):
            calls.append((path, kwargs["vad_filter"]))
            return iter([SimpleNamespace(
                start=0.0, end=1.0, text="か", avg_logprob=math.log(0.8), no_speech_prob=0.01,
            )]), SimpleNamespace(language="ja")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.0)))
    monkeypatch.setattr(lyric_recognition, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(lyric_recognition, "decode_kana_window", lambda *args: ("カ", 0.9))
    matrix = CTCEmissions(np.zeros((500, 2)), {"<pad>": 0, "カ": 1})
    result, returned = lyric_recognition.transcribe_multiview(
        Path("vocals.wav"), Path("mix.wav"), emissions=matrix,
    )
    assert returned is matrix
    assert len(models) == 1
    assert calls == [("vocals.wav", True), ("mix.wav", True),
                     ("vocals.wav", False), ("mix.wav", False)]
    assert len(result.hypotheses) == 4
    assert all(x.confidence == pytest.approx(0.8) for x in result.hypotheses)
    assert all(x.timing_confidence == 0 for x in result.acoustic)
    assert result.selected_hypotheses


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


def test_overlapping_asr_windows_keep_all_original_candidates(monkeypatch):
    class Whisper:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            return iter([SimpleNamespace(
                start=start, end=end, text="か", avg_logprob=math.log(.8), no_speech_prob=.01,
            ) for start, end in [(0., 2.), (1.5, 3.), (3., 5.)]]), SimpleNamespace(language="ja")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=5.)))
    monkeypatch.setattr(lyric_recognition, "reading_candidates", lambda text: ["カ"])
    monkeypatch.setattr(lyric_recognition, "decode_kana_window", lambda *args: ("カ", .9))
    matrix = CTCEmissions(np.zeros((250, 2)), {"<pad>": 0, "カ": 1})
    result, _ = lyric_recognition.transcribe_multiview(
        Path("vocals.wav"), Path("vocals.wav"), emissions=matrix,
    )
    first_pass = [h for h in result.hypotheses if h.vad_enabled]
    assert [(h.start_sec, h.end_sec) for h in first_pass] == [(0., 2.), (1.5, 3.), (3., 5.)]
    assert first_pass[0].pass_id == first_pass[2].pass_id
    assert first_pass[1].pass_id != first_pass[0].pass_id
    by_id = {h.id: h for h in result.hypotheses}
    assert all(a.stream_id == by_id[a.id.removeprefix("ctc:")].pass_id for a in result.acoustic)


def test_known_lyrics_evidence_path_never_calls_whisper(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, mora_align, pitch, reading, transcribe
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.mora_align import AlignedMora

    def forbidden(*args, **kwargs):
        pytest.fail("known lyrics must not call ASR")

    monkeypatch.setattr(transcribe, "transcribe_lines", forbidden)
    monkeypatch.setattr(lyric_recognition, "transcribe_multiview", forbidden)
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
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *args, **kwargs: None)
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": False, "rmvpe": False, "fcpe": False,
    })
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき", encoding="utf-8")
    value = analyze_audio(tmp_path / "input.wav", tmp_path / "project", lyrics_path=lyrics,
                          device="cpu", skip_separation=True, lyric_pipeline="evidence")
    assert value.lyric_layers["canonical_text"] == "かき"
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert [x["confidence"] for x in value.lyric_layers["performed"]] == [0.75, 0.65]


def test_partial_recognition_windows_survive_alignment_and_voiced_extension(monkeypatch, tmp_path):
    from wav_to_xf import ReadingCandidate
    from wav_to_xf.recognition import (
        AcousticPronunciation,
        RecognitionHypothesis,
        fuse_unknown_lyrics,
    )

    from soramimic_video import audio_melody, mora_align, pitch, reading
    from soramimic_video.analyze_audio import analyze_audio
    from soramimic_video.mora_align import AlignedMora

    hypotheses = tuple(RecognitionHypothesis(
        f"h{i}", "pass", "synthetic-asr", "vocals", True, str(i), start, start + 1,
        "ja", .9, 0, kana, (ReadingCandidate(kana, "synthetic", 1),),
    ) for i, (start, kana) in enumerate([(1., "カ"), (8., "キ")]))
    acoustic = tuple(AcousticPronunciation(
        f"a{i}", "synthetic-ctc", "vocals", h.start_sec, h.end_sec, .9, 0, kana=h.surface,
    ) for i, h in enumerate(hypotheses))
    result = fuse_unknown_lyrics(hypotheses, acoustic, duration_sec=10.)
    emissions = object()
    monkeypatch.setattr(lyric_recognition, "transcribe_multiview",
                        lambda *a, **kw: (result, emissions))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda path: SimpleNamespace(duration=10.)))
    monkeypatch.setattr(reading, "reading_candidates",
                        lambda *a: pytest.fail("keep selected reading"))

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
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *a, **kw: None)
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {
        "sheetsage2": False, "rmvpe": False, "fcpe": False,
    })
    value = analyze_audio(tmp_path / "input.wav", tmp_path / "project", device="cpu",
                          skip_separation=True, lyric_pipeline="evidence")
    assert extensions == [(1.2, 2.), (8.2, 9.)]
    assert [n.end_sec for n in value.notes] == [2., 9.]
    assert [n.kana for n in value.notes] == ["カ", "キ"]
    assert value.lyric_layers["canonical_text"] == "カ\nキ"
