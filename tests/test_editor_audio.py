import json
from contextlib import contextmanager

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


def test_fixed_json_alignment_preserves_readings_rows_and_assignments(monkeypatch, tmp_path):
    import soramimic_score
    from soramimic_score import AlignedMora, AudioAdapters, MelodyNote, media, models

    from soramimic_video.editor_io import import_editor
    from soramimic_video.project import Project
    from soramimic_video.score_audio import analyze_audio

    def forbidden(*_args, **_kwargs):
        pytest.fail("JSON mode must not recognize lyrics or select new readings")

    def align(_path, lines, readings):
        assert [line.text for line in lines] == ["生", "生"]
        assert all(line.start_sec is None and line.end_sec is None for line in lines)
        assert [r.kana for r in readings] == ["ナマ", "セイ"]
        return tuple(AlignedMora(li, mi, c, (li * 2 + mi) * .3,
                                (li * 2 + mi + 1) * .3, .9)
                     for li, reading in enumerate(readings) for mi, c in enumerate(reading.kana))

    adapters = AudioAdapters(
        lyric_recognizer=forbidden, reading_selector=forbidden,
        supplied_reading_selector=forbidden, mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .3, (i + 1) * .3, 60)
                                          for i in range(4)),
    )

    @contextmanager
    def prepared(_path, _config, **kwargs):
        assert kwargs["accompaniment_path"].name == "no_vocals.wav"
        yield adapters

    @contextmanager
    def decoded(path):
        yield path

    monkeypatch.setattr(models, "prepared_adapters", prepared)
    monkeypatch.setattr(media, "decoded_audio", decoded)
    monkeypatch.setattr(soramimic_score, "analyze_audio", forbidden)
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
    assert [line.original_text for line in project.lines] == ["生", "", "生"]
    assert [line.canonical_kana for line in project.lines] == ["ナマ", "", "セイ"]
    assert [note.line for note in project.notes] == [0, 0, 2, 2]
    assert all(note.end_sec <= 1.2 and note.midi_note == 60 for note in project.notes)
    import_editor(project, tmp_path, editor)
    assert project.parody.lines[0].words[0].surface == "名前"
    assert project.parody.lines[0].words[0].locked
    assert project.parody.lines[0].words[0].note_ids == [0, 1]
    assert project.parody.lines[1].words == []
    project.save(tmp_path)
    restored = Project.load(tmp_path)
    assert [line.original_text for line in restored.lines] == ["生", "", "生"]
    assert json.loads(editor.read_text()) == data
    analysis = json.loads((tmp_path / "analyze_audio/analysis.json").read_text())
    assert analysis["asr_used"] is False
    assert analysis["fixed_editor_readings"] is True
    assert all("認識" not in stage for stage in stages)


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
