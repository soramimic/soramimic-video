"""Acceptance coverage for the live automatic-lyrics conversion lifecycle."""

from __future__ import annotations

from pathlib import Path


def test_automatic_lyrics_stage3_result_reaches_weighted_conversion_without_reload(
    monkeypatch,
    tmp_path: Path,
):
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        api,
        audio_melody,
        mix,
        mora_align,
        reading,
        stage3,
        synthesize,
        video,
    )
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.project import Project
    from soramimic_video.transcribe import TranscribedLine

    # Keep the acceptance test deterministic at external model boundaries.  The
    # application pipeline, analysis assembly, layer import, and conversion remain
    # real, including the in-memory handoff that production uses before any reload.
    monkeypatch.setattr(analyze_audio_module, "_require_audio_pipeline", lambda: None)
    monkeypatch.setattr(
        audio_melody,
        "configured_capabilities",
        lambda: {"sheetsage2": True},
    )
    monkeypatch.setattr(
        analyze_audio_module,
        "_run_audio_models",
        lambda audio_path, *_args, **_kwargs: (
            audio_path,
            None,
            [TranscribedLine(0.0, 0.5, "かき")],
            [MelodyNote(0.0, 0.25, 60), MelodyNote(0.25, 0.5, 62)],
        ),
    )
    monkeypatch.setattr(reading, "automatic_reading_candidates", lambda _text: ["カキ"])
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *_args, **_kwargs: (
            [
                AlignedMora(0, 0, "カ", 0.05, 0.15, 0.8),
                AlignedMora(0, 1, "キ", 0.30, 0.40, 0.8),
            ],
            [0],
        ),
    )

    class Stage3Document:
        def to_json(self) -> str:
            return '{"note_candidates": [], "links": []}'

    class Stage3Realization:
        """Match Realization.to_dict's tuple-valued in-memory representation."""

        unresolved_unit_ids: tuple[str, ...] = ()

        def to_dict(self):
            return {
                "schema_version": 1,
                "canonical_text": "かき",
                "canonical": (
                    {
                        "utterance_id": "u0",
                        "text": "かき",
                        "kana": "カキ",
                        "mora_ids": ("m0", "m1"),
                    },
                ),
                "performed": (
                    {
                        "singing_unit_id": "s0",
                        "mora_ids": ("m0",),
                        "status": "observed",
                        "start_sec": 0.0,
                        "end_sec": 0.25,
                        "confidence": 0.8,
                        "link_ids": ("l0",),
                        "evidence_ids": (),
                    },
                    {
                        "singing_unit_id": "s1",
                        "mora_ids": ("m1",),
                        "status": "observed",
                        "start_sec": 0.25,
                        "end_sec": 0.5,
                        "confidence": 0.8,
                        "link_ids": ("l1",),
                        "evidence_ids": (),
                    },
                ),
                "synthesis_plan": (
                    {
                        "id": "slot-0",
                        "utterance_id": "u0",
                        "singing_unit_id": "s0",
                        "mora_ids": ("m0",),
                        "note_candidate_id": "n0",
                        "link_ids": ("l0",),
                        "kana": "カ",
                        "start_sec": 0.0,
                        "end_sec": 0.25,
                        "midi_pitch": 60,
                        "operation": "match",
                        "timing_source": "note_interval",
                        "confidence": 0.8,
                        "evidence_ids": (),
                        "pitch_sources": ("sheetsage2-vocal",),
                        "continuation": False,
                    },
                    {
                        "id": "slot-1",
                        "utterance_id": "u0",
                        "singing_unit_id": "s1",
                        "mora_ids": ("m1",),
                        "note_candidate_id": "n1",
                        "link_ids": ("l1",),
                        "kana": "キ",
                        "start_sec": 0.25,
                        "end_sec": 0.5,
                        "midi_pitch": 62,
                        "operation": "match",
                        "timing_source": "note_interval",
                        "confidence": 0.8,
                        "evidence_ids": (),
                        "pitch_sources": ("sheetsage2-vocal",),
                        "continuation": False,
                    },
                ),
                "omissions": (),
                "unresolved_unit_ids": (),
                "diagnostics": (),
                "evidence": (),
            }

    monkeypatch.setattr(
        stage3,
        "build_stage3_layers",
        lambda *_args, **_kwargs: (Stage3Document(), Stage3Realization()),
    )

    real_analyze_audio = analyze_audio_module.analyze_audio

    def analyze_without_external_separation(*args, **kwargs):
        kwargs["skip_separation"] = True
        return real_analyze_audio(*args, **kwargs)

    monkeypatch.setattr(analyze_audio_module, "analyze_audio", analyze_without_external_separation)

    def fake_synthesize(project, project_dir, **_kwargs):
        assert project.parody is not None
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
    (job_dir / "input.wav").write_bytes(b"deterministic-model-boundary")
    wordlist = tmp_path / "words.csv"
    wordlist.write_text(
        "id,original,surface,pronunciation\n0,柿,柿,カキ",
        encoding="utf-8",
    )
    job = api.Job(
        id="automatic-lyrics-e2e",
        dir=job_dir,
        params={
            "input_kind": "audio",
            "input_seconds": 0.5,
            "auto_lyrics": True,
            "wordlist": str(wordlist),
            "convert_params": "NOTE_LENGTH_WEIGHT=0.25",
            "model": "MERROW",
            "synthesizer": "voicevox",
        },
    )

    output = api.run_pipeline(job, {"parallel_video": False})

    assert output.read_bytes() == b"video"
    assert [stage["name"] for stage in job.stages] == [
        "analyze",
        "convert",
        "synthesize",
        "mix",
        "video",
    ]
    saved = Project.load(job_dir)
    assert saved.lyric_layers is not None
    assert isinstance(saved.lyric_layers["canonical"], list)
    assert saved.parody is not None
    assert [word.surface for word in saved.parody.lines[0].words] == ["柿"]
