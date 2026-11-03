"""Retention, isolation and failure provenance for public diagnostics."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from soramimic_video import api, error_diagnostics
from test_public_mode import submit, wait_done


@pytest.fixture
def failed_job(tmp_path, monkeypatch):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, "24")
    monkeypatch.setattr(api, "song_seconds", lambda _: 0.0)

    def pipeline(job, config):
        with api._stage(job, "convert"):
            (job.dir / "project.json").write_text('{"reading":"ネX"}')
            job.thumbnail.write_bytes(b"private-thumbnail")
            logging.getLogger("soramimic_video.convert").warning("private-word ネX")
            logging.getLogger("soramimic.maker").warning("private-core-word ネX")
            raise ValueError("private-error ネX")

    monkeypatch.setattr(api, "run_pipeline", pipeline)
    app = api.create_app(jobs_dir=tmp_path / "jobs")
    client = TestClient(app)
    response = submit(client, lyrics="private-lyrics")
    assert response.status_code == 200
    job_id = response.json()["id"]
    result = wait_done(client, job_id)
    assert result["status"] == "error"
    return client, app.state.manager, app.state.manager.jobs[job_id]


def test_private_report_keeps_context_without_exposing_files(failed_job, caplog):
    client, manager, job = failed_job
    report_path = job.dir / error_diagnostics.REPORT_FILENAME
    report = json.loads(report_path.read_text())
    assert report["stage"] == "convert"
    assert report["exception_type"] == "ValueError"
    assert report["error"] == "private-error ネX"
    assert "ValueError: private-error ネX" in report["traceback"]
    assert any("private-word ネX" in line for line in report["log"])
    assert any("private-core-word ネX" in line for line in report["log"])
    assert "soramimic-yomi" in report["versions"]
    assert "revision" in report["versions"]["soramimic-yomi"]
    assert "owner" not in report
    assert "client_hash" not in report
    assert (job.dir / "input.mid").is_file()
    assert (job.dir / "lyrics.txt").read_text() == "private-lyrics"
    assert (job.dir / "project.json").is_file()
    assert stat.S_IMODE(job.dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert job.cleanup_pending is False
    assert not job.log
    assert manager.pending_cleanup_count() == 0
    response = client.get(f"/api/jobs/{job.id}").json()
    assert response["error"] == "生成に失敗しました"
    assert "log" not in response
    assert "thumbnail_url" not in response
    for path in ("thumbnail", "video", "playback", "lyrics", "credits", "exports/vocal"):
        assert client.get(f"/api/jobs/{job.id}/{path}").status_code in (404, 409)
    assert client.get(f"/api/jobs/{job.id}/{error_diagnostics.REPORT_FILENAME}").status_code == 404
    records = [
        record for record in caplog.get_records("setup")
        if record.name.startswith("soramimic")
    ]
    assert records
    for record in records:
        assert "private-" not in record.getMessage()
        assert record.exc_info is None


@pytest.mark.parametrize("hours", [0, 1, 24, 72])
def test_diagnostics_expire_within_48h_independent_of_result_ttl(failed_job, monkeypatch, hours):
    client, manager, job = failed_job
    monkeypatch.setenv(api.JOB_TTL_HOURS_ENV, str(hours))
    expires = job.diagnostics_expires_at
    assert expires is not None
    assert expires <= job.finished_at + 48 * 3600
    assert manager.cleanup_expired(now=expires - 1) == []
    assert (job.dir / "input.mid").is_file()
    assert manager.cleanup_expired(now=expires) == [job.id]
    assert not job.dir.exists()
    assert client.get(f"/api/jobs/{job.id}").status_code == 404


def test_restart_keeps_original_deadline_and_removes_expired_diagnostics(failed_job):
    _, manager, job = failed_job
    deadline = job.diagnostics_expires_at
    report = (job.dir / error_diagnostics.REPORT_FILENAME).read_bytes()
    restarted = api.create_app(jobs_dir=manager.jobs_dir).state.manager
    loaded = restarted.jobs[job.id]
    assert loaded.diagnostics_expires_at == deadline
    assert (loaded.dir / error_diagnostics.REPORT_FILENAME).read_bytes() == report
    assert (loaded.dir / "input.mid").is_file()
    loaded.diagnostics_expires_at = time.time() - 1
    restarted._save(loaded)
    again = api.create_app(jobs_dir=manager.jobs_dir).state.manager
    assert job.id not in again.jobs
    assert not job.dir.exists()


def test_restart_recovers_report_written_before_status_commit(failed_job):
    _, manager, job = failed_job
    report = (job.dir / error_diagnostics.REPORT_FILENAME).read_bytes()
    path = job.dir / api.STATUS_FILENAME
    saved = json.loads(path.read_text())
    saved["status"] = "running"
    saved.pop("diagnostics_expires_at")
    saved.pop("finished_at")
    saved["error"] = None
    path.write_text(json.dumps(saved))
    os.utime(path, (job.finished_at - 3, job.finished_at - 3))
    restarted = api.create_app(jobs_dir=manager.jobs_dir).state.manager
    loaded = restarted.jobs[job.id]
    assert loaded.error == "private-error ネX"
    assert loaded.diagnostics_expires_at <= job.diagnostics_expires_at
    assert (loaded.dir / error_diagnostics.REPORT_FILENAME).read_bytes() == report


def test_expired_diagnostics_deletion_failure_is_hidden_and_retried(failed_job, monkeypatch):
    client, manager, job = failed_job
    original = api.shutil.rmtree

    def locked(path, *args, **kwargs):
        if path == job.dir:
            raise PermissionError("private-path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.shutil, "rmtree", locked)
    assert manager.cleanup_expired(now=job.diagnostics_expires_at) == [job.id]
    assert job.dir.is_dir()
    assert manager.pending_cleanup_count() == 1
    assert client.get(f"/api/jobs/{job.id}").status_code == 404
    monkeypatch.setattr(api.shutil, "rmtree", original)
    manager._retry_pending_deletions()
    assert not job.dir.exists()
    assert manager.pending_cleanup_count() == 0


@pytest.mark.parametrize("failure_type", [OSError, ValueError, RuntimeError])
def test_report_failure_still_retains_inputs_with_a_durable_deadline(
    tmp_path, monkeypatch, failure_type,
):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setattr(api, "song_seconds", lambda _: 0.0)

    def no_space(*args, **kwargs):
        raise failure_type("cannot save private-error")

    def pipeline(job, config):
        raise RuntimeError("original-failure")

    monkeypatch.setattr(error_diagnostics, "save_report", no_space)
    monkeypatch.setattr(api, "run_pipeline", pipeline)
    app = api.create_app(jobs_dir=tmp_path / "jobs")
    client = TestClient(app)
    job_id = submit(client).json()["id"]
    wait_done(client, job_id)
    job = app.state.manager.jobs[job_id]
    saved = json.loads((job.dir / api.STATUS_FILENAME).read_text())
    assert saved["diagnostics_expires_at"] == job.diagnostics_expires_at
    assert (job.dir / "input.mid").is_file()
    assert job.error == "original-failure"


def test_parallel_failure_preserves_originating_stage(tmp_path):
    job = api.Job(id="abcdef12", dir=tmp_path, params={})
    failure = RuntimeError("encoding failed")
    error_diagnostics.annotate_stage(failure, "video")
    with pytest.raises(RuntimeError):
        with api._stage(job, "synthesize"):
            raise failure
    assert error_diagnostics.failure_stage(failure, job.stage) == "video"


def test_stage_annotation_does_not_replace_an_immutable_exception(tmp_path):
    class ImmutableError(Exception):
        def __setattr__(self, name, value):
            raise AttributeError("immutable")

    failure = ImmutableError("original failure")
    job = api.Job(id="abcdef12", dir=tmp_path, params={})
    with pytest.raises(ImmutableError, match="original failure"):
        with api._stage(job, "analyze"):
            raise failure


def test_subprocess_report_keeps_captured_stderr_without_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SORAMIMIC_VIDEO_API_KEY", "never-collect-this")
    error_diagnostics.save_report(
        tmp_path,
        stage="analyze",
        error="decoder failed",
        exc=subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"bad frame"),
        failed_at=100,
        expires_at=200,
        started_at=90,
        stages=[],
        params={},
        log=[],
    )
    text = (tmp_path / error_diagnostics.REPORT_FILENAME).read_text()
    assert json.loads(text)["stderr"] == "bad frame"
    assert "never-collect-this" not in text
