"""Result deletion, ownership, durable cleanup and admission limits."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soramimic_video import api


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.delenv(api.PUBLIC_ENV, raising=False)
    return TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))


def saved_job(browser, status="done", suffix=".mp4"):
    manager = browser.app.state.manager
    directory = manager.jobs_dir / "abcde123"
    directory.mkdir()
    output = directory / f"result{suffix}"
    output.write_bytes(b"result")
    (directory / "input.wav").write_bytes(b"private input")
    intermediate = directory / "intermediates"
    intermediate.mkdir()
    (intermediate / "lyrics.txt").write_text("private lyrics")
    job = api.Job(
        id=directory.name, dir=directory, params={"song_title": "private title"},
        status=status, video=output, finished_at=time.time(),
        owner=browser.cookies.get(api.SESSION_COOKIE), client_hash="private-hash",
    )
    manager.jobs[job.id] = job
    manager._save(job)
    return job


@pytest.mark.parametrize(("status", "suffix"), [
    ("done", ".mp4"), ("done", ".wav"), ("error", ".mp4"), ("canceled", ".mp4"),
])
def test_delete_result_removes_files_and_survives_restart(browser, status, suffix):
    job = saved_job(browser, status, suffix)
    manager = browser.app.state.manager
    response = browser.delete(f"/api/jobs/{job.id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "cleanup_pending": False}
    assert list(job.dir.iterdir()) == [job.dir / api.STATUS_FILENAME]
    receipt = json.loads((job.dir / api.STATUS_FILENAME).read_text())
    assert set(receipt) == {"id", "status", "created_at", "client_hash"}
    assert receipt["status"] == "deleted"
    assert browser.get("/api/jobs").json() == []
    for resource in ("", "/video", "/playback", "/thumbnail", "/lyrics",
                     "/credits", "/exports/midi", "/review"):
        assert browser.get(f"/api/jobs/{job.id}{resource}").status_code == 404
    # Old output requests and canceled queue entries cannot restore result data.
    manager.record_output_usage(job, "playback")
    manager._save(job)
    manager._run_one(job)
    assert json.loads((job.dir / api.STATUS_FILENAME).read_text()) == receipt
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 200
    restarted = TestClient(api.create_app(jobs_dir=manager.jobs_dir))
    assert restarted.get("/api/jobs").json() == []
    assert restarted.get(f"/api/jobs/{job.id}").status_code == 404
    restarted.app.state.manager.cleanup_expired(now=job.created_at + 86401)
    assert not job.dir.exists()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_review"])
def test_delete_rejects_active_jobs(browser, status):
    job = saved_job(browser, status)
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 409
    assert job.video.read_bytes() == b"result"
    assert browser.get(f"/api/jobs/{job.id}").status_code == 200


def test_delete_requires_api_key(browser, monkeypatch):
    job = saved_job(browser)
    monkeypatch.setenv(api.API_KEY_ENV, "deletion-test-key")
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 401
    assert browser.delete(f"/api/jobs/{job.id}", headers={
        "X-API-Key": "deletion-test-key",
    }).status_code == 200


def test_delete_preserves_shared_files_and_does_not_follow_symlinks(browser, tmp_path):
    job = saved_job(browser)
    shared = tmp_path / "shared-list"
    shared.mkdir()
    (shared / "list.csv").write_text("shared")
    (job.dir / "linked-list").symlink_to(shared, target_is_directory=True)
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 200
    assert (shared / "list.csv").read_text() == "shared"


def test_delete_is_durable_and_retries_file_failures(browser, monkeypatch):
    job = saved_job(browser)
    original = api.shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path).name == "intermediates":
            raise PermissionError("temporarily unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.shutil, "rmtree", fail)
    response = browser.delete(f"/api/jobs/{job.id}")
    assert response.status_code == 202
    assert response.json()["cleanup_pending"] is True
    assert browser.get("/api/jobs").json() == []
    restarted = TestClient(api.create_app(jobs_dir=browser.app.state.manager.jobs_dir))
    assert restarted.get(f"/api/jobs/{job.id}").status_code == 404
    manager = restarted.app.state.manager
    manager.cleanup_expired(now=job.created_at + 86401)
    assert job.dir.exists(), "failed cleanup must retain its durable retry marker"
    monkeypatch.setattr(api.shutil, "rmtree", original)
    manager._scrub_terminal_jobs(pending_only=True)
    assert list(job.dir.iterdir()) == [job.dir / api.STATUS_FILENAME]
    manager.cleanup_expired(now=job.created_at + 86401)
    assert not job.dir.exists()


def test_delete_cannot_race_a_late_status_write(browser):
    job = saved_job(browser)
    manager = browser.app.state.manager
    ready = threading.Event()
    release = threading.Event()

    def late_write():
        ready.set()
        assert release.wait(5)
        manager._save(job)

    thread = threading.Thread(target=late_write)
    thread.start()
    assert ready.wait(5)
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 200
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert json.loads((job.dir / api.STATUS_FILENAME).read_text())["status"] == "deleted"


def test_deleted_submission_cannot_be_recreated(browser):
    job = saved_job(browser)
    job.submission_id = "a" * 32
    browser.app.state.manager._save(job)
    assert browser.delete(f"/api/jobs/{job.id}").status_code == 200
    for app in (browser.app, api.create_app(jobs_dir=job.dir.parent)):
        response = TestClient(app).post("/api/jobs", data={
            "submission_id": job.submission_id, "sample_id": "furusato", "wordlist": "stations",
        })
        assert response.status_code == 410, response.text
        assert len(app.state.manager.jobs) == 1


def test_receipt_write_failure_preserves_result(browser, monkeypatch):
    job = saved_job(browser)
    save = browser.app.state.manager._save

    def fail(receipt):
        if receipt.status == "deleted":
            raise PermissionError("cannot save deletion")
        save(receipt)

    monkeypatch.setattr(browser.app.state.manager, "_save", fail)
    response = TestClient(browser.app, raise_server_exceptions=False).delete(f"/api/jobs/{job.id}")
    assert response.status_code == 500
    assert browser.get(f"/api/jobs/{job.id}").status_code == 200
    assert job.video.read_bytes() == b"result"


@pytest.mark.parametrize("limit", ["session", "ip"])
def test_public_delete_checks_owner_and_preserves_daily_limits(tmp_path, monkeypatch, limit):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setenv(api.DAILY_QUOTA_ENV, "1" if limit == "session" else "0")
    monkeypatch.setenv(api.IP_DAILY_QUOTA_ENV, "1" if limit == "ip" else "0")
    monkeypatch.setenv(api.IP_HASH_KEY_ENV, "stable-test-key")
    app = api.create_app(jobs_dir=tmp_path / "jobs")
    alice, bob = TestClient(app), TestClient(app)
    alice.get("/api/config")
    job = saved_job(alice)
    # Use the same IP identity as the public submission endpoint.
    job.client_hash = hmac.new(b"stable-test-key", b"unknown", hashlib.sha256).hexdigest()[:16]
    app.state.manager._save(job)
    assert bob.delete(f"/api/jobs/{job.id}").status_code == 404
    assert alice.delete(f"/api/jobs/{job.id}").status_code == 200
    assert bob.delete(f"/api/jobs/{job.id}").status_code == 404
    for current in (app, api.create_app(jobs_dir=tmp_path / "jobs")):
        browser = TestClient(current)
        browser.cookies.update(alice.cookies)
        manager = current.state.manager
        assert manager.recent_count(job.owner, time.time() - 86400) == 1
        assert manager.recent_client_count(job.client_hash, time.time() - 86400) == 1
        response = browser.post("/api/jobs", data={
            "sample_id": "furusato", "wordlist": "stations",
        })
        assert response.status_code == 429, response.text

