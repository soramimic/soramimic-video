"""Application-owned search deadlines and private timeout diagnostics."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from soramimic_video import api, error_diagnostics, score_audio, search_limits
from test_public_mode import FAKE_MP4, submit, wait_done


def test_configured_search_timeout(monkeypatch):
    monkeypatch.delenv(search_limits.SEARCH_TIMEOUT_ENV, raising=False)
    assert search_limits.configured_timeout() == 120.
    monkeypatch.setenv(search_limits.SEARCH_TIMEOUT_ENV, "45.5")
    assert search_limits.configured_timeout() == 45.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_search_timeout_is_rejected(monkeypatch, value):
    monkeypatch.setenv(search_limits.SEARCH_TIMEOUT_ENV, value)
    with pytest.raises(ValueError, match=search_limits.SEARCH_TIMEOUT_ENV):
        search_limits.configured_timeout()


@pytest.mark.parametrize("editor", [False, True])
def test_search_timeout_retains_private_diagnostics_and_releases_worker(
    monkeypatch, tmp_path, editor,
):
    import soramimic_score.media
    import soramimic_score.models
    from soramimic_score import (
        AlignedMora,
        AudioAdapters,
        LyricLine,
        MelodyNote,
        ReadingSelection,
        note_runs,
    )
    from soramimic_score.japanese import kana_to_moras

    clock = 0.
    evaluations = 0
    original = note_runs._assignment_cost

    def align(_path, _lines, readings):
        nonlocal clock
        # Model time must not consume the note-correspondence deadline.
        clock += 1000.
        return tuple(AlignedMora(li, mi, kana, li + mi * .1 + .01,
                                 li + mi * .1 + .09, .9)
                     for li, reading in enumerate(readings)
                     for mi, kana in enumerate(kana_to_moras(reading.kana)))

    def evaluate(*args, **kwargs):
        nonlocal clock, evaluations
        evaluations += 1
        clock += 121.
        return original(*args, **kwargs)

    adapters = AudioAdapters(
        reading_selector=lambda _path, _lines: (ReadingSelection("カ", "synthetic", 1),),
        mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .1, i * .1 + .09, 60)
                                          for i in range(600)),
        lyric_recognizer=lambda _: (LyricLine("カ", 0, 60),),
    )

    @contextmanager
    def prepared(*args, **kwargs):
        yield adapters

    @contextmanager
    def decoded(path):
        yield path

    first = True

    def pipeline(job, config):
        nonlocal first
        if first:
            first = False
            source = job.dir / "input.wav"
            source.write_bytes(b"synthetic private audio")
            editor_path = None
            if editor:
                from test_editor_audio import payload

                editor_path = job.dir / "editor.json"
                editor_path.write_text(json.dumps(payload(), ensure_ascii=False))
            with api._stage(job, "analyze"):
                score_audio.analyze_audio(source, job.dir, device="cpu", editor_path=editor_path)
            pytest.fail("expired search returned a completed project")
        out = job.dir / "song.mp4"
        out.write_bytes(FAKE_MP4)
        return out

    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, "24")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/synthetic")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/synthetic-base")
    monkeypatch.delenv(search_limits.SEARCH_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(note_runs, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(note_runs, "_assignment_cost", evaluate)
    monkeypatch.setattr(soramimic_score.models, "prepared_adapters", prepared)
    monkeypatch.setattr(soramimic_score.media, "decoded_audio", decoded)
    monkeypatch.setattr(api, "song_seconds", lambda _: 0.)
    monkeypatch.setattr(api, "run_pipeline", pipeline)
    client = TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))

    response = submit(client, lyrics="private supplied lyrics")
    job_id = response.json()["id"]
    result = wait_done(client, job_id)
    assert result["status"] == "error"
    assert result["error"] == search_limits.SEARCH_TIMEOUT_MESSAGE
    assert evaluations > 0, "model time must not trigger the search deadline"
    assert "log" not in result
    manager = client.app.state.manager
    job = manager.jobs[job_id]
    report = json.loads((job.dir / error_diagnostics.REPORT_FILENAME).read_text())
    assert report["exception_type"] == "AudioSearchTimeout"
    assert report["stage"] == "analyze"
    assert report["search_timeout"]["operation"] == "lyric_note_search"
    assert report["search_timeout"]["limit_seconds"] == 120.
    assert report["search_timeout"]["elapsed_seconds"] >= 121.
    assert report["expires_at"] <= job.finished_at + 48 * 3600
    assert (job.dir / "input.wav").is_file()
    assert not (job.dir / "analyze_audio" / "score.json").exists()
    assert client.get(f"/api/jobs/{job_id}/{error_diagnostics.REPORT_FILENAME}").status_code == 404
    assert wait_done(client, submit(client).json()["id"])["status"] == "done"
    manager.cleanup_expired(now=report["expires_at"] - 1)
    assert job_id in manager.jobs
    assert job_id in manager.cleanup_expired(now=report["expires_at"])
    assert not job.dir.exists()
