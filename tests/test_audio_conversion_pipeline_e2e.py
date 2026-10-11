"""Exercise the current Score-to-video pipeline at external model boundaries."""

from __future__ import annotations

import json
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest
from soramimic_score import AlignedMora, AudioAdapters, LyricLine, MelodyNote, ReadingSelection
from soramimic_score import models as score_models

from soramimic_video import api, mix, synthesize, video
from soramimic_video.project import Project
from soramimic_video.voicevox import build_score


@pytest.mark.parametrize("supplied", [False, True])
def test_score_result_reaches_weighted_conversion_without_reload(
    monkeypatch, tmp_path: Path, supplied: bool,
):
    expected_text = "柿" if supplied else "かき"
    observed = []

    def select_reading(_path, lines):
        assert [line.text for line in lines] == [expected_text]
        return (ReadingSelection("カキ", "test", 1),)

    adapters = AudioAdapters(
        lyric_recognizer=lambda _: (LyricLine("かき", 0, .5),),
        lyric_reading=lambda _: "カキ",
        reading_selector=select_reading,
        mora_aligner=lambda *_: (
            AlignedMora(0, 0, "カ", .05, .15, .8),
            AlignedMora(0, 1, "キ", .30, .40, .8),
        ),
        melody_transcriber=lambda _: (MelodyNote(0, .25, 60), MelodyNote(.25, .5, 62)),
    )

    @contextmanager
    def prepare_models(path, config, *, accompaniment_path):
        observed.append(path)
        assert config.separate_vocals is True
        assert accompaniment_path == job_dir / "analyze_audio/no_vocals.wav"
        accompaniment_path.write_bytes(path.read_bytes())
        yield adapters

    # Keep API orchestration, Score analysis, layer import and conversion real.
    # Only model inference, synthesis and media rendering are replaced.
    monkeypatch.setattr(score_models, "prepared_adapters", prepare_models)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")

    def fake_synthesize(project, project_dir, **_kwargs):
        assert project.parody is not None
        assert project.lines[0].original_text == expected_text
        notes = build_score(project)["notes"]
        assert [note["lyric"] for note in notes if note["key"] is not None] == ["カ", "キ"]
        output = project_dir / "synthesis" / "vocal.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"vocal")
        return output

    def fake_mix(_project, project_dir, **_kwargs):
        output = project_dir / "mix" / "mix.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mix")
        return output

    def fake_video(project, project_dir, **_kwargs):
        assert project.parody is not None
        output = project_dir / "video" / "out.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return output

    monkeypatch.setattr(synthesize, "synthesize", fake_synthesize)
    monkeypatch.setattr(mix, "mix", fake_mix)
    monkeypatch.setattr(video, "make_video", fake_video)

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with wave.open(str(job_dir / "input.wav"), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 8000)
    if supplied:
        (job_dir / "lyrics.txt").write_text(expected_text, encoding="utf-8")
    wordlist = tmp_path / "words.csv"
    wordlist.write_text("id,original,surface,pronunciation\n0,柿,柿,カキ", encoding="utf-8")
    job = api.Job(
        id="score-conversion-e2e",
        dir=job_dir,
        params={
            "input_kind": "audio", "input_seconds": .5, "auto_lyrics": not supplied,
            "wordlist": str(wordlist), "convert_params": "NOTE_LENGTH_WEIGHT=0.25",
            "model": "MERROW", "synthesizer": "voicevox",
        },
    )

    with monkeypatch.context() as patch:
        patch.setattr(Project, "load", lambda *_: pytest.fail("Unexpected project reload"))
        output = api.run_pipeline(job, {"parallel_video": False})

    assert output.read_bytes() == b"video"
    assert observed == [job_dir / "input.wav"]
    assert [stage["name"] for stage in job.stages] == [
        "analyze", "convert", "synthesize", "mix", "video",
    ]
    saved = Project.load(job_dir)
    assert saved.lyric_layers is not None
    assert saved.lyric_layers["canonical_text"] == expected_text
    assert saved.lines[0].original_text == expected_text
    assert saved.parody is not None
    assert [word.surface for word in saved.parody.lines[0].words] == ["柿"]
    assert [(note.kana, note.midi_note) for note in saved.notes] == [("カ", 60), ("キ", 62)]
    analysis = json.loads((job_dir / "analyze_audio/analysis.json").read_text())
    assert analysis["audio_pipeline"] == "soramimic-score"
    assert analysis["official_lyrics"] is supplied
