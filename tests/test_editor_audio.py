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


@pytest.mark.parametrize("skip_separation", [False, True])
@pytest.mark.parametrize("token_surfaces", [False, True])
def test_json_alignment_uses_fixed_readings_without_recognition(
    monkeypatch, tmp_path, skip_separation, token_surfaces,
):
    import soramimic_score
    import soramimic_score.media
    import soramimic_score.models
    from soramimic_score import AlignedMora, AudioAdapters, MelodyNote
    from soramimic_score.japanese import kana_to_moras

    from soramimic_video.editor_io import import_editor
    from soramimic_video.project import Project
    from soramimic_video.score_audio import analyze_audio

    aligned_readings = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("fixed JSON must not recognize lyrics or select readings")

    def align(_path, lines, readings):
        aligned_readings.append("".join(r.kana for r in readings))
        assert [line.text for line in lines] == ["生", "生"]
        assert all(line.start_sec is None and line.end_sec is None for line in lines)
        assert [r.detail["editor_line_index"] for r in readings] == [0, 2]
        result = []
        offset = 0
        for li, reading in enumerate(readings):
            moras = kana_to_moras(reading.kana)
            result.extend(AlignedMora(li, mi, c, (offset + mi) * .3,
                                     (offset + mi + 1) * .3, .99)
                          for mi, c in enumerate(moras))
            offset += len(moras)
        return tuple(result)

    adapters = AudioAdapters(
        lyric_recognizer=forbidden, reading_selector=forbidden,
        lyric_reading=forbidden, supplied_reading_selector=forbidden,
        audio_duration=lambda _: 1.2, mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .3, (i + 1) * .3, 60)
                                          for i in range(4)),
    )
    @contextmanager
    def prepared(path, config, *, accompaniment_path):
        assert config.separate_vocals is not skip_separation
        assert (accompaniment_path is None if skip_separation
                else accompaniment_path.name == "no_vocals.wav")
        yield adapters

    @contextmanager
    def decoded(path):
        yield path

    monkeypatch.setattr(soramimic_score.models, "prepared_adapters", prepared)
    monkeypatch.setattr(soramimic_score.media, "decoded_audio", decoded)
    monkeypatch.setattr(soramimic_score, "analyze_audio", forbidden)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    words = tmp_path / "words.csv"
    words.write_text("id,original,surface,pronunciation\n0,名前,名前,ナマ\n")
    data = {**payload(), "lyrics": "別の古い歌詞", "wordlist": {"filepath": str(words)},
            "notes": [{"start_sec": 900, "midi_pitch": 1}]}
    if token_surfaces:
        data.update(phrases=["ナマ", "", "セイ"],
                    tokensList=[[{"surface_form": "生"}], [], [{"surface_form": "生"}]])
    editor = tmp_path / "editor.json"
    editor.write_text(json.dumps(data, ensure_ascii=False))
    source = tmp_path / "input.wav"
    source.touch()
    stages = []
    project = analyze_audio(source, tmp_path, editor_path=editor, device="cpu",
                            progress_detail=stages.append, skip_separation=skip_separation)
    assert aligned_readings == ["ナマセイ"]
    assert [line["text"] for line in project.lyric_layers["canonical"]] == ["生", "生"]
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
    assert analysis["asr_used"] is False
    assert analysis["fixed_editor_readings"] is True
    receipt = json.loads((tmp_path / "analyze_audio/lyric_input.json").read_text())
    assert receipt["lines"][1] == {"text": "", "reading": ""}
    assert not any("認識" in stage for stage in stages)


@pytest.mark.parametrize("change", [
    {"phrases": ["生"]}, {"unitsList": [[{}], [], [{"pronunciation": "セイ"}]]},
    {"unitsList": [[{"pronunciation": "漢字"}], [], [{"pronunciation": "セイ"}]]},
    {"unitsList": [[], [], []]}, {"phrases": ["生", "読めない行", "生"]},
])
def test_invalid_json_readings_fail_before_models(change):
    with pytest.raises(ValueError):
        editor_audio_lines({**payload(), **change})


def test_json_original_lines_keep_phrase_identity():
    data = {**payload(), "originalLines": ["なま", "", "せい"], "tokensList": [[], [], []]}
    assert editor_audio_lines(data) == [("なま", "ナマ"), ("", ""), ("せい", "セイ")]
