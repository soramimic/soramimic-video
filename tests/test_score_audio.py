import copy
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from soramimic_video import score_audio


def test_score_audio_imports_supported_slots_and_records_unresolved_units(monkeypatch, tmp_path):
    layers = {
        "schema_version": 1, "canonical_text": "かき", "canonical": [
            {"utterance_id": "u0", "text": "かき", "kana": "カキ",
             "mora_ids": ["m0", "m1"]}],
        "performed": [
            {"singing_unit_id": f"s{i}", "mora_ids": [f"m{i}"],
             "start_sec": i * .3, "end_sec": (i + 1) * .3} for i in range(2)],
        "synthesis_plan": [
            {"utterance_id": "u0", "singing_unit_id": "s0", "mora_ids": ["m0"],
             "kana": "カ", "start_sec": 0., "end_sec": .3, "midi_pitch": 60,
             "pitch_sources": ["sheetsage2-vocal"]}],
        "omissions": [], "unresolved_unit_ids": ["s1"], "diagnostics": [], "evidence": [],
    }
    observed = {}

    def fake_analyze(path, **kwargs):
        observed.update(kwargs)
        assert path == tmp_path / "input.wav"
        kwargs["on_progress"]("歌詞を認識しています")
        return SimpleNamespace(
            to_dict=lambda: {"score": copy.deepcopy(layers)},
            to_json=lambda: json.dumps({"score": layers}),
        )

    import soramimic_score

    monkeypatch.setattr(soramimic_score, "analyze_audio", fake_analyze)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"fake")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき\n", encoding="utf-8")
    values = []
    project = score_audio.analyze_audio(source, tmp_path / "project", lyrics_path=lyrics,
                                        progress=values.append)

    assert observed["lyrics"] == ["かき"]
    assert observed["accompaniment_path"] == tmp_path / "project/analyze_audio/no_vocals.wav"
    assert [note.kana for note in project.notes] == ["カ"]
    assert project.lines[0].canonical_kana == "カキ"
    assert project.lyric_layers["unresolved_unit_ids"] == []
    assert len(project.lyric_layers["omissions"]) == 1
    assert values[0] == .01 and values[-1] == 1.
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["audio_pipeline"] == "soramimic-score"
    assert analysis["diagnostics"][0]["unit_count"] == 1
    assert json.loads((tmp_path / "project/analyze_audio/score.json").read_text())[
        "score"]["unresolved_unit_ids"] == ["s1"]


def test_score_audio_requires_positive_bpm_and_lyrics_for_adjustment(tmp_path):
    with pytest.raises(ValueError, match="入力歌詞"):
        score_audio.analyze_audio(tmp_path / "audio.wav", tmp_path, adjust_lyrics=True)
    with pytest.raises(ValueError, match="BPM"):
        score_audio.analyze_audio(tmp_path / "audio.wav", tmp_path, bpm=0)


@pytest.mark.parametrize("control", ["\0", "\x85"])
def test_score_audio_rejects_controls_before_splitting_lines_or_loading_models(tmp_path, control):
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("か\u3099" + control + "くせい", encoding="utf-8")
    with pytest.raises(ValueError, match=f"position 3:.*U\\+{ord(control):04X}"):
        score_audio.analyze_audio(tmp_path / "input.wav", tmp_path, lyrics_path=lyrics)


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("source_text", ["か\u3099くせい", "カ゛クセイ"])
def test_pinned_score_normalizes_before_real_reading_generation(
    monkeypatch, tmp_path, supplied, source_text,
):
    import soramimic_score
    from soramimic_score import AlignedMora, AudioAdapters, LyricLine, MelodyNote
    from soramimic_score.readings import dictionary_readings

    from soramimic_video.project import Project

    def align(_path, lines, readings):
        assert lines[0].text == source_text
        assert readings[0].kana == "ガクセイ"
        return tuple(AlignedMora(0, i, mora, i * .3, (i + 1) * .3, .9)
                     for i, mora in enumerate("ガクセイ"))

    adapters = AudioAdapters(
        reading_selector=dictionary_readings,
        mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .3, (i + 1) * .3, 60)
                                            for i in range(4)),
        lyric_recognizer=lambda _: (LyricLine(source_text, 0, 1.2),),
    )
    real_analyze = soramimic_score.analyze_audio
    monkeypatch.setattr(soramimic_score, "analyze_audio",
                        lambda path, **kw: real_analyze(path, adapters, lyrics=kw["lyrics"]))
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic audio model boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text(source_text, encoding="utf-8")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output, lyrics_path=lyrics if supplied else None)
    project.save(output)
    reloaded = Project.load(output)
    assert reloaded.lyric_layers["canonical_text"] == source_text
    assert reloaded.lines[0].canonical_kana == "ガクセイ"
    assert "".join(note.kana for note in reloaded.notes) == "ガクセイ"
    assert lyrics.read_text(encoding="utf-8") == source_text
    raw = json.loads((output / "analyze_audio/score.json").read_text())
    decision = next(e["detail"] for e in raw["observations"]["evidence"]
                    if e["kind"] == "reading-selection")
    assert decision["lyric_input_normalization"]["original_text"] == source_text


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("kana,normalized,moras", [
    ("ウォォォ", "ウォォォ", ("ウォ", "ォ", "ォ")),
    ("ｶﾞゐ", "ガイ", ("ガ", "イ")),
    ("ヷ", "ヴァ", ("ヴァ",)),
])
def test_pinned_score_normalizes_readings_through_audio_import(
    monkeypatch, tmp_path, supplied, kana, normalized, moras,
):
    import soramimic_score
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
    )

    from soramimic_video.project import Project

    def align(_path, _lines, selected):
        assert selected[0].kana == normalized
        return tuple(AlignedMora(0, i, mora, i * .5, (i + 1) * .5, .9)
                     for i, mora in enumerate(moras))

    adapters = AudioAdapters(
        lyric_recognizer=lambda _: (LyricLine(kana, 0, len(moras) * .5),),
        reading_selector=lambda *_: (ReadingSelection(kana, "test", 1),),
        mora_aligner=align,
        melody_transcriber=lambda _: tuple(
            MelodyNote(i * .5, (i + 1) * .5, 60, confidence=.9) for i in range(len(moras))),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        assert kwargs["lyrics"] == ([kana] if supplied else None)
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"])

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text(kana, encoding="utf-8")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output, lyrics_path=lyrics if supplied else None)
    project.save(output)
    reloaded = Project.load(output)
    assert reloaded.lyric_layers["canonical_text"] == kana
    assert reloaded.lines[0].canonical_kana == normalized
    assert "".join(note.kana for note in reloaded.notes) == normalized
    assert [(note.start_sec, note.end_sec) for note in reloaded.notes] == [
        (i * .5, (i + 1) * .5) for i in range(len(moras))]
    if kana != normalized:
        raw = json.loads((output / "analyze_audio/score.json").read_text())
        decision = next(e["detail"] for e in raw["observations"]["evidence"]
                        if e["kind"] == "reading-selection")
        assert decision["reading_normalization"]["selected_before"] == kana


@pytest.mark.parametrize("adjust", [False, True])
@pytest.mark.parametrize("source_text,recognized,expected_text,kana,status", [
    ("未来（みらい）（ラララ）", "未来（ラララ）", "未来（ラララ）", "ミライラララ", "resolved"),
    ("運命（さだめ）", "運命", "運命", "サダメ", "resolved"),
    ("誰だ（だれだ）", "誰だ誰だ", "誰だ（だれだ）", "ダレダダレダ", "retained-sung"),
])
def test_pinned_score_uses_parenthetical_reading_and_keeps_chorus(
    monkeypatch, tmp_path, adjust, source_text, recognized, expected_text, kana, status,
):
    import soramimic_score
    from soramimic_score import AlignedMora, LyricLine, MelodyNote, ModelConfig
    from soramimic_score.japanese import kana_to_moras
    from soramimic_score.models import create_adapters
    from soramimic_score.vocal_activity import VocalActivity

    from soramimic_video.convert import engine_phrases

    monkeypatch.setattr(ModelConfig, "validate", lambda _: None)
    monkeypatch.setitem(sys.modules, "librosa", SimpleNamespace(get_duration=lambda **_: 2.))
    monkeypatch.setattr("soramimic_score.models.transcribe_kana_views",
                        lambda *_, **__: {"mix": (kana,), "vocals": (kana,)})

    def align(_path, lines, readings):
        assert [reading.kana for reading in readings] == [kana]
        return tuple(AlignedMora(0, index, mora, .1 + index * .3, .3 + index * .3, .9)
                     for index, mora in enumerate(kana_to_moras(readings[0].kana)))

    adapters = replace(
        create_adapters(ModelConfig(tmp_path, tmp_path, device="cpu"),
                        vocals_path=tmp_path / "vocals.wav"),
        mora_aligner=align,
        melody_transcriber=lambda _: tuple(
            MelodyNote(.1 + i * .3, .3 + i * .3, 60, confidence=.9)
            for i in range(len(kana_to_moras(kana)))),
        lyric_recognizer=lambda _: (LyricLine(recognized, 0, 2),),
        audio_duration=lambda _: 2.,
        vocal_activity=lambda _path, windows: tuple(
            VocalActivity(-20, -3, 1, True) for _ in windows),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        assert kwargs["lyrics"] == [source_text]
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"],
                            adjust_lyrics=kwargs["adjust_lyrics"])

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text(source_text, encoding="utf-8")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output, lyrics_path=lyrics, adjust_lyrics=adjust)
    assert project.lyric_layers["canonical_text"] == expected_text
    assert project.lines[0].canonical_kana == kana
    assert "".join(note.kana for note in project.notes) == kana
    assert engine_phrases(project) == [kana]
    assert lyrics.read_text(encoding="utf-8") == source_text
    annotations = json.loads((output / "analyze_audio/lyric_annotations.json").read_text())
    assert annotations["supplied_lines"] == [source_text]
    decision = annotations["resolved_groups"][0]["parenthetical_readings"][0]
    assert decision["status"] == status


@pytest.mark.parametrize("with_melody", [True, False])
def test_pinned_score_speech_reaches_voicevox_once_without_losing_sung_notes(
    monkeypatch, tmp_path, with_melody,
):
    import soramimic_score
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
    )
    from soramimic_score.vocal_activity import VocalActivity

    from soramimic_video import analyze_audio as legacy_audio
    from soramimic_video.project import Project
    from soramimic_video.voicevox import build_score

    melody = (MelodyNote(0, .4, 65), MelodyNote(3, 3.4, 67)) if with_melody else ()
    adapters = AudioAdapters(
        reading_selector=lambda _path, lines: tuple(
            ReadingSelection(line.text, "test", 1) for line in lines),
        mora_aligner=lambda *_args: (
            AlignedMora(0, 0, "カ", .05, .15, .8),
            AlignedMora(1, 0, "キ", 1.96, 1.98, 1e-8),
            AlignedMora(1, 1, "ク", 2, 2.02, 1e-8),
            AlignedMora(2, 0, "ケ", 3.05, 3.2, .8),
        ),
        melody_transcriber=lambda _path: melody,
        lyric_recognizer=lambda _path: (
            LyricLine("カ", 0, .4), LyricLine("キク", 1, 2.4), LyricLine("ケ", 3, 3.4),
        ),
        vocal_activity=lambda _path, windows: tuple(
            VocalActivity(-20, -3, 1, True) for _ in windows),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"],
                            on_progress=kwargs["on_progress"])

    def no_duplicate_recovery(*_args, **_kwargs):
        pytest.fail("Score output must not run through legacy speech recovery again")

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setattr(legacy_audio, "_recover_synthesis_units", no_duplicate_recovery)
    monkeypatch.setattr(legacy_audio, "_continuize_spoken_synthesis_lines", no_duplicate_recovery)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output)

    assert [note.kana for note in project.notes] == list("カキクケ")
    assert [(note.start_sec, note.end_sec) for note in project.notes[1:3]] == [
        (1, 1.7), (1.7, 2.4),
    ]
    assert all(note.source == "spoken" and note.pitch_confidence is None
               for note in project.notes[1:3])
    if with_melody:
        assert [(note.start_sec, note.end_sec, note.midi_note)
                for note in project.notes if note.source != "spoken"] == [
            (0, .4, 65), (3, 3.4, 67),
        ]
    else:
        assert all(note.source == "spoken" for note in project.notes)
    assert project.lyric_layers["omissions"] == []
    assert project.lyric_layers["unresolved_unit_ids"] == []
    saved = json.loads((output / "analyze_audio/score.json").read_text())
    assert saved["score"]["synthesis_plan"] == project.lyric_layers["synthesis_plan"]
    project.save(output)
    reloaded = Project.load(output)
    assert reloaded.notes == project.notes
    synth = build_score(reloaded)
    assert [note["lyric"] for note in synth["notes"] if note["key"] is not None] == list("カキクケ")
    analysis = json.loads((output / "analyze_audio/analysis.json").read_text())
    assert analysis["sources"]["spoken"] == (2 if with_melody else 4)
    assert analysis["limitations"] == []


@pytest.mark.parametrize("complete_lyrics", [False, True])
def test_pinned_score_preserves_supplied_lyrics_through_save_and_synthesis(
    monkeypatch, tmp_path, complete_lyrics,
):
    import soramimic_score
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
    )
    from soramimic_score.vocal_activity import VocalActivity

    from soramimic_video.project import Project
    from soramimic_video.voicevox import build_score

    supplied = ["ア", "カ", "キ", "ク", "ケ", "オ"]
    aligned = []

    def align(_path, lines, readings):
        aligned.extend(line.text for line in lines)
        return tuple(AlignedMora(i, 0, reading.kana,
                                 line.start_sec + .05, line.start_sec + .25, .9)
                     for i, (line, reading) in enumerate(zip(lines, readings, strict=True)))

    adapters = AudioAdapters(
        reading_selector=lambda _path, lines: tuple(
            ReadingSelection(line.text, "test", 1) for line in lines),
        mora_aligner=align,
        melody_transcriber=lambda _: tuple(
            MelodyNote(start + .05, start + .25, 60 + i, confidence=.9)
            for i, start in enumerate([0, 1, 2, 4, 5, 6, 7])),
        lyric_recognizer=lambda _: (
            LyricLine("カ", 1, 2), LyricLine("ズ", 2, 3), LyricLine("ク", 4, 5),
            LyricLine("コ", 5, 6), LyricLine("ケ", 6, 7),
        ),
        audio_duration=lambda _: 8.,
        vocal_activity=lambda _path, windows: tuple(
            VocalActivity(-20, -3, 1, True) for _ in windows),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"],
                            adjust_lyrics=kwargs["adjust_lyrics"])

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("\n".join(supplied), encoding="utf-8")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output, lyrics_path=lyrics,
                                        adjust_lyrics=complete_lyrics)
    expected = ["ア", "カ", "キ", "ク", "コ", "ケ", "オ"] if complete_lyrics else supplied
    assert aligned == expected
    assert project.lyric_layers["canonical_text"] == "\n".join(expected)
    assert [note.kana for note in project.notes] == expected
    project.save(output)
    reloaded = Project.load(output)
    assert reloaded.lyric_layers == project.lyric_layers
    synth = build_score(reloaded)
    assert [note["lyric"] for note in synth["notes"] if note["key"] is not None] == expected


@pytest.mark.parametrize("adjust", [False, True])
@pytest.mark.parametrize("all_silent", [False, True])
def test_pinned_score_removes_only_verified_absent_lyrics_when_enabled(
    monkeypatch, tmp_path, adjust, all_silent,
):
    import soramimic_score
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
    )
    from soramimic_score.vocal_activity import VocalActivity

    from soramimic_video.project import Project

    supplied = ["カキ", "サシ", "タチ"]
    heard = () if all_silent else (LyricLine("カキ", 0, .4), LyricLine("タチ", 2, 2.4))
    notes = () if all_silent else tuple(
        MelodyNote(start, start + .2, 60 + i, confidence=.9)
        for i, start in enumerate([0, .2, 2, 2.2]))
    adapters = AudioAdapters(
        reading_selector=lambda _path, lines: tuple(
            ReadingSelection(line.text.replace("\n", ""), "test", 1) for line in lines),
        mora_aligner=lambda _path, lines, readings: tuple(
            AlignedMora(i, j, kana, line.start_sec + j * .2 + .05,
                        line.start_sec + j * .2 + .1, .9)
            for i, (line, reading) in enumerate(zip(lines, readings, strict=True))
            for j, kana in enumerate(reading.kana)),
        melody_transcriber=lambda _: notes,
        lyric_recognizer=lambda _: heard,
        audio_duration=lambda _: 2.4,
        vocal_activity=lambda _path, windows: tuple(
            VocalActivity(-100, -90, 0, False, silence_confirmed=True) for _ in windows),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"],
                            adjust_lyrics=kwargs["adjust_lyrics"])

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("\n".join(supplied), encoding="utf-8")
    output = tmp_path / "project"
    if adjust and all_silent:
        with pytest.raises(ValueError, match="歌詞が残りません"):
            score_audio.analyze_audio(source, output, lyrics_path=lyrics, adjust_lyrics=adjust)
    else:
        project = score_audio.analyze_audio(
            source, output, lyrics_path=lyrics, adjust_lyrics=adjust)
        project.save(output)
        expected = ["カキ", "タチ"] if adjust else supplied
        assert Project.load(output).lyric_layers["canonical_text"] == "\n".join(expected)
    raw = json.loads((output / "analyze_audio/score.json").read_text())
    surface = next(e["detail"] for e in raw["observations"]["evidence"]
                   if e["kind"] == "lyric-surface")
    assert surface["supplied_lines"] == supplied
    expected_removed = ([0, 1, 2] if all_silent else [1]) if adjust else []
    assert surface["removed_supplied_indices"] == expected_removed
    assert lyrics.read_text(encoding="utf-8") == "\n".join(supplied)


@pytest.mark.parametrize("with_intro_notes", [True, False])
def test_pinned_score_keeps_note_supported_recovery_and_reports_uncertain_alignment(
    monkeypatch, tmp_path, with_intro_notes,
):
    import soramimic_score
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
    )
    from soramimic_score.japanese import kana_to_moras

    from soramimic_video.project import Project
    from soramimic_video.voicevox import build_score

    notes = tuple(MelodyNote(i * .5, (i + 1) * .5, 60 + i, source="sheetsage2-vocal")
                  for i in range(4))
    adapters = AudioAdapters(
        reading_selector=lambda _path, lines: tuple(
            ReadingSelection(line.text, "test", 1) for line in lines),
        mora_aligner=lambda _path, lines, readings: tuple(
            AlignedMora(i, j, mora, line.start_sec + j * .5,
                        line.start_sec + (j + 1) * .5, 1e-5 if line.start_sec < 2 else .9)
            for i, (line, reading) in enumerate(zip(lines, readings, strict=True))
            for j, mora in enumerate(kana_to_moras(reading.kana))),
        melody_transcriber=lambda _: (
            *(notes if with_intro_notes else ()),
            MelodyNote(2.2, 2.7, 65, source="sheetsage2-vocal")),
        lyric_recognizer=lambda _: (
            LyricLine("作詞・作曲・編曲 初音ミク", 0, 2), LyricLine("コ", 2.2, 2.7)),
        lyric_recoverer=lambda _path, start, end: (LyricLine("カキクケ", start, end),),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze_with_test_models(path, **kwargs):
        assert kwargs["lyrics"] is None
        return real_analyze(path, adapters)

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze_with_test_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"deterministic model boundary")
    output = tmp_path / "project"
    project = score_audio.analyze_audio(source, output)
    project.save(output)
    reloaded = Project.load(output)
    expected = "カキクケコ" if with_intro_notes else "コ"
    assert "".join(note.kana for note in reloaded.notes) == expected
    assert "".join(note["lyric"] for note in build_score(reloaded)["notes"]
                   if note["key"] is not None) == expected
    analysis = json.loads((output / "analyze_audio/analysis.json").read_text())
    assert analysis["generation_quality"]["status"] == (
        "warning" if with_intro_notes else "ok")
    if with_intro_notes:
        assert analysis["generation_quality"]["reason"] == "uncertain-lyric-alignment"
        assert analysis["generation_quality"]["uncertain_lyric_lines"] == 1
        assert analysis["diagnostics"][-1]["evidence_ids"] == ["audio-ctc-note-supported-0"]
        assert len(analysis["limitations"]) == 1
    else:
        assert analysis["limitations"] == []
