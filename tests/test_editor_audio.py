import json

import pytest

from soramimic_video.editor_audio import editor_audio_lines


def payload():
    return {
        "phrases": ["生", "", "生"],
        "unitsList": [[{"pronunciation": c} for c in "ナマ"], [],
                      [{"pronunciation": c} for c in "セイ"]],
        "results": [[{"id": "0", "surface": "名前", "pronunciation": ["ナ", "マ"],
                      "period": [0, 2], "kana": "ナマ", "locked": True}], [],
                    [{"id": "0", "surface": "名前", "pronunciation": ["ナ", "マ"],
                      "period": [0, 2], "kana": "ナマ", "locked": True}]],
    }


def test_json_alignment_localizes_then_forces_known_readings(monkeypatch, tmp_path):
    import soramimic_score
    from soramimic_score import AlignedMora, AudioAdapters, LyricLine, MelodyNote
    from soramimic_score.japanese import kana_to_moras
    from soramimic_score.readings import dictionary_readings

    from soramimic_video.editor_io import import_editor
    from soramimic_video.project import Project
    from soramimic_video.score_audio import analyze_audio

    recognized = []
    aligned_readings = []

    def recognize(_path):
        recognized.append(True)
        return (LyricLine("なませい", 0., 1.2),)

    def align(_path, lines, readings):
        aligned_readings.append("".join(r.kana for r in readings))
        assert all(line.start_sec is not None and line.end_sec is not None for line in lines)
        result = []
        for li, (line, reading) in enumerate(zip(lines, readings, strict=True)):
            moras = kana_to_moras(reading.kana)
            duration = (line.end_sec - line.start_sec) / len(moras)
            result.extend(AlignedMora(li, mi, c, line.start_sec + mi * duration,
                                     line.start_sec + (mi + 1) * duration, .99)
                          for mi, c in enumerate(moras))
        return tuple(result)

    adapters = AudioAdapters(
        lyric_recognizer=recognize, reading_selector=dictionary_readings,
        lyric_reading=lambda text: dictionary_readings(None, (LyricLine(text),))[0].kana,
        audio_duration=lambda _: 1.2, mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .3, (i + 1) * .3, 60)
                                          for i in range(4)),
    )
    real_analyze = soramimic_score.analyze_audio

    def analyze(path, **kwargs):
        assert kwargs["accompaniment_path"].name == "no_vocals.wav"
        return real_analyze(path, adapters, lyrics=kwargs["lyrics"],
                            on_progress=kwargs["on_progress"])

    monkeypatch.setattr(soramimic_score, "analyze_audio", analyze)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    words = tmp_path / "words.csv"
    words.write_text("id,original,surface,pronunciation\n0,名前,名前,ナマ\n")
    data = {**payload(), "lyrics": "別の古い歌詞", "wordlist": {"filepath": str(words)},
            "notes": [{"start_sec": 900, "midi_pitch": 1}]}
    editor = tmp_path / "editor.json"
    editor.write_text(json.dumps(data, ensure_ascii=False))
    source = tmp_path / "input.wav"
    source.touch()
    stages = []
    project = analyze_audio(source, tmp_path, editor_path=editor, device="cpu",
                            progress_detail=stages.append)
    assert recognized == [True]
    assert "ナマセイ" in aligned_readings
    assert "".join(line.canonical_kana for line in project.lines) == "ナマセイ"
    assert all(note.end_sec <= 1.2 and note.midi_note == 60 for note in project.notes)
    import_editor(project, tmp_path, editor)
    edited = [word for row in project.parody.lines for word in row.words]
    assert [word.surface for word in edited] == ["名前", "名前"]
    assert all(word.locked for word in edited)
    assert edited[0].note_ids == [0, 1]
    assert edited[1].note_ids == [2, 3]
    project.save(tmp_path)
    assert Project.load(tmp_path) == project
    assert json.loads(editor.read_text()) == data
    analysis = json.loads((tmp_path / "analyze_audio/analysis.json").read_text())
    assert analysis["asr_used"] is True
    assert analysis["fixed_editor_readings"] is True
    receipt = json.loads((tmp_path / "analyze_audio/lyric_input.json").read_text())
    assert receipt["lines"][1] == {"text": "", "reading": ""}
    assert any("認識" in stage for stage in stages)


@pytest.mark.parametrize("change", [
    {"phrases": ["生"]}, {"unitsList": [[{}], [], [{"pronunciation": "セイ"}]]},
    {"unitsList": [[{"pronunciation": "漢字"}], [], [{"pronunciation": "セイ"}]]},
    {"unitsList": [[], [], []]}, {"phrases": ["生", "読めない行", "生"]},
])
def test_invalid_json_readings_fail_before_models(change):
    with pytest.raises(ValueError):
        editor_audio_lines({**payload(), **change})


def test_json_original_lines_keep_phrase_identity():
    data = {**payload(), "originalLines": ["なま", "", "せい"]}
    assert editor_audio_lines(data) == [("なま", "ナマ"), ("", ""), ("せい", "セイ")]
