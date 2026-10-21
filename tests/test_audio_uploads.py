"""Resumable audio bytes, authorization, retention, and ordinary job admission."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api  # noqa: E402
from soramimic_video.audio_uploads import CHUNK_BYTES, AudioUploads  # noqa: E402

SID = "a" * 32


def wav(seconds=0.1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * round(8000 * seconds))
    return output.getvalue()


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setattr(api, "audio_input_available", lambda: True)

    def pipeline(job, config):
        output = job.dir / "out.mp4"
        output.write_bytes(b"finished")
        return output

    monkeypatch.setattr(api, "run_pipeline", pipeline)
    return TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))


def begin(browser, data, sid=SID, filename="song.wav", **extra):
    return browser.post("/api/audio-uploads", data={
        "filename": filename, "size": len(data), "submission_id": sid, **extra,
    })


def put(browser, data, offset=0, sid=SID):
    return browser.put(f"/api/audio-uploads/{sid}?offset={offset}", content=data)


def submit(browser, sid=SID, **extra):
    return browser.post("/api/jobs", data={
        "submission_id": sid, "audio_upload_id": sid, "wordlist": "stations", **extra,
    })


def wait_done(browser, job_id):
    for _ in range(200):
        body = browser.get(f"/api/jobs/{job_id}").json()
        if body["status"] not in {"queued", "running"}:
            return body
        time.sleep(.01)
    pytest.fail("job did not finish")


def test_transfer_resumes_after_restart_and_duplicate_ack_loss(tmp_path):
    root = tmp_path / "uploads"
    store = AudioUploads(root, 100)
    store.create(SID, "owner", "ip", "song.mp3", 12)
    store.append(SID, "owner", 0, b"first-")
    store = AudioUploads(root, 100)
    assert store.info(SID, "owner")["offset"] == 6
    with ThreadPoolExecutor(2) as executor:
        responses = list(executor.map(lambda _: store.append(SID, "owner", 6, b"second"), range(2)))
    assert [r["offset"] for r in responses] == [12, 12]
    path, name = store.completed(SID, "owner")
    assert name == "song.mp3" and path.read_bytes() == b"first-second"
    with pytest.raises(HTTPException) as error:
        store.append(SID, "owner", 0, b"wrong-")
    assert error.value.status_code == 409
    assert path.read_bytes() == b"first-second"


def test_small_chunks_return_one_job_and_remove_private_audio(browser, monkeypatch):
    monkeypatch.setenv(api.TURNSTILE_SECRET_ENV, "secret")
    tokens = []
    monkeypatch.setattr(api, "verify_turnstile", lambda token, ip=None: not tokens.append(token)
                        and len(tokens) == 1 and token == "once")
    data = wav()
    assert begin(browser, data, turnstile_token="once").status_code == 200
    assert put(browser, data[:100]).json()["offset"] == 100
    # Retrying creation does not spend another single-use verification token.
    assert begin(browser, data, turnstile_token="expired").json()["offset"] == 100
    assert put(browser, data[100:], 100).status_code == 200
    first = submit(browser, turnstile_token="expired")
    assert first.status_code == 200, first.text
    job_id = first.json()["id"]
    assert submit(browser).json()["id"] == job_id
    assert begin(browser, data).json()["job_id"] == job_id
    assert tokens == ["once"]
    assert len(browser.app.state.manager.jobs) == 1
    assert wait_done(browser, job_id)["status"] == "done"
    assert browser.get(f"/api/jobs/{job_id}/playback").content == b"finished"
    assert not (browser.app.state.audio_uploads.root / SID).exists()
    job = browser.app.state.manager.jobs[job_id]
    assert not (job.dir / "input.wav").exists()


def test_stage_cannot_bypass_verification_or_owner(browser, monkeypatch):
    data = wav()
    monkeypatch.setenv(api.TURNSTILE_SECRET_ENV, "secret")
    monkeypatch.setattr(api, "verify_turnstile", lambda token, ip=None: token == "good")
    assert begin(browser, data).status_code == 403
    assert begin(browser, data, turnstile_token="good").status_code == 200
    other = TestClient(browser.app)
    path = f"/api/audio-uploads/{SID}"
    assert other.get(path).status_code == 404
    assert put(other, data).status_code == 404
    assert other.delete(path).status_code == 404
    assert submit(other).status_code == 404
    assert begin(other, data, turnstile_token="good").status_code == 404
    assert browser.get(path).json()["offset"] == 0
    assert browser.get(path).headers["cache-control"] == "private, no-store"


def test_api_key_protects_all_transfer_operations(browser, monkeypatch):
    assert begin(browser, wav()).status_code == 200
    monkeypatch.setenv(api.API_KEY_ENV, "key")
    assert begin(browser, wav()).status_code == 401
    assert put(browser, b"x").status_code == 401
    assert browser.get(f"/api/audio-uploads/{SID}").status_code == 401
    assert browser.delete(f"/api/audio-uploads/{SID}").status_code == 401
    assert submit(browser).status_code == 401


def test_cancel_incomplete_and_invalid_input_never_create_jobs(browser):
    data = wav()
    assert begin(browser, data).status_code == 200
    assert submit(browser).status_code == 409
    assert put(browser, data[:100], offset=1).status_code == 409
    assert browser.delete(f"/api/audio-uploads/{SID}").status_code == 200
    assert submit(browser).status_code == 404
    assert not browser.app.state.manager.jobs
    assert begin(browser, b"bad").status_code == 200
    assert put(browser, b"bad").status_code == 200
    assert submit(browser).status_code == 400
    assert not (browser.app.state.audio_uploads.root / SID).exists()


def test_expiry_and_failed_removal_are_retried(tmp_path, monkeypatch):
    store = AudioUploads(tmp_path / "uploads", 100)
    store.create(SID, "owner", "ip", "song.mp3", 20)
    store.append(SID, "owner", 0, b"private")
    real_remove = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("busy")))
    with pytest.raises(OSError):
        store.remove(SID, "owner")
    assert store.cleanup_failed
    monkeypatch.setattr(shutil, "rmtree", real_remove)
    store.cleanup()
    assert not store.cleanup_failed and not (store.root / SID).exists()
    store.create(SID, "owner", "ip", "song.mp3", 20)
    store.cleanup(now=time.time() + 3601)
    assert not (store.root / SID).exists()


def test_capacity_reserves_declared_bytes_and_limits_cookie_rotation(tmp_path):
    store = AudioUploads(tmp_path / "uploads", 100)
    for n in range(2):
        store.create(f"{n:032x}", str(n), "shared-ip", "song.mp3", 100)
    with pytest.raises(HTTPException) as error:
        store.create("f" * 32, "new-cookie", "shared-ip", "song.mp3", 1)
    assert error.value.status_code == 429
    for n in range(2, 4):
        store.create(f"{n:032x}", str(n), str(n), "song.mp3", 100)
    with pytest.raises(HTTPException) as error:
        store.create("f" * 32, "other", "other", "song.mp3", 1)
    assert error.value.status_code == 429


def test_chunk_and_declared_size_limits_do_not_advance_offset(browser):
    data = wav()
    assert begin(browser, data).status_code == 200
    assert put(browser, b"x" * (CHUNK_BYTES + 1)).status_code == 413
    assert put(browser, b"x" * (len(data) + 1)).status_code == 413
    response = browser.put(f"/api/audio-uploads/{SID}?offset=0",
                           content=iter([b"x" * CHUNK_BYTES, b"x"]),
                           headers={"Content-Length": "1"})
    assert response.status_code == 413
    assert begin(browser, b"x", sid="../bad").status_code == 422
    assert browser.get(f"/api/audio-uploads/{SID}").json()["offset"] == 0
    assert submit(browser, submission_id="b" * 32).status_code == 422


def test_final_admission_still_enforces_daily_and_duration_limits(browser, monkeypatch):
    data = wav(2)
    assert begin(browser, data).status_code == 200
    assert put(browser, data).status_code == 200
    monkeypatch.setenv(api.MAX_SONG_SECONDS_ENV, "1")
    assert submit(browser).status_code == 400
    assert not browser.app.state.manager.jobs
    monkeypatch.setenv(api.MAX_SONG_SECONDS_ENV, "420")
    browser.delete(f"/api/audio-uploads/{SID}")
    assert begin(browser, wav()).status_code == 200
    assert put(browser, wav()).status_code == 200
    assert submit(browser).status_code == 200
    monkeypatch.setenv(api.DAILY_QUOTA_ENV, "1")
    assert begin(browser, wav(), sid="b" * 32).status_code == 429


def test_restart_cleans_stage_if_process_stopped_after_job_persistence(browser):
    data = wav()
    assert begin(browser, data).status_code == 200
    assert put(browser, data).status_code == 200
    manager = browser.app.state.manager
    job = manager.create(None, None, "", {"wordlist": "stations", "input_kind": "audio"},
                         owner=browser.cookies[api.SESSION_COOKIE], submission_id=SID, audio=data)
    assert wait_done(browser, job.id)["status"] == "done"
    assert (browser.app.state.audio_uploads.root / SID).exists()
    restarted = api.create_app(jobs_dir=manager.jobs_dir)
    assert not (restarted.state.audio_uploads.root / SID).exists()
    assert job.id in restarted.state.manager.jobs


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_four_minute_mp3_uses_original_audio_and_existing_normalization(browser, tmp_path):
    path = tmp_path / "four-minutes.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=240", "-ac", "1", "-ar", "8000",
                    "-c:a", "libmp3lame", "-b:a", "16k", str(path)], check=True)
    data = path.read_bytes()
    assert begin(browser, data, filename=path.name).status_code == 200
    for offset in range(0, len(data), 65536):
        assert put(browser, data[offset:offset + 65536], offset).status_code == 200
    owner = browser.cookies[api.SESSION_COOKIE]
    stage, name = browser.app.state.audio_uploads.completed(SID, owner)
    assert name == path.name and stage.read_bytes() == data
    result = submit(browser)
    assert result.status_code == 200, result.text
    assert wait_done(browser, result.json()["id"])["status"] == "done"


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_browser_transfer_recovery():
    subprocess.run(["node", "--test", "tests/audio-upload.mjs"], check=True, capture_output=True,
                   text=True)


@pytest.mark.skipif(not shutil.which("node") or not shutil.which("ffmpeg"),
                    reason="node and ffmpeg required")
def test_real_http_recovers_four_minute_mp3_after_body_and_acknowledgement_loss(
    browser, tmp_path, monkeypatch,
):
    import hashlib
    import socket
    import threading

    import uvicorn

    path = tmp_path / "http-four-minutes.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=240", "-ac", "1", "-ar", "8000",
                    "-c:a", "libmp3lame", "-b:a", "16k", str(path)], check=True)
    expected = hashlib.sha256(path.read_bytes()).digest()
    seen = []
    original = api.resolve_song_input

    async def resolve(midi, audio, sample_id):
        seen.append(hashlib.sha256(await audio.read()).digest())
        await audio.seek(0)
        resolved = await original(midi, audio, sample_id)
        assert resolved[2] == pytest.approx(240, abs=.2)
        return resolved

    monkeypatch.setattr(api, "resolve_song_input", resolve)
    monkeypatch.setenv(api.TURNSTILE_SECRET_ENV, "secret")
    tokens = []
    monkeypatch.setattr(api, "verify_turnstile", lambda token, ip=None: not tokens.append(token)
                        and len(tokens) == 1 and token == "once")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(browser.app, log_level="error", lifespan="off")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if server.started:
                    break
                time.sleep(.01)
            assert server.started
            result = subprocess.run([
                "node", "tests/audio-upload-http.mjs",
                f"http://127.0.0.1:{sock.getsockname()[1]}", str(path),
            ], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            receipt = json.loads(result.stdout)
            assert receipt["resumed"] is True
            owner = browser.app.state.manager.jobs[receipt["job_id"]].owner
            assert owner is not None
            browser.cookies.set(api.SESSION_COOKIE, owner)
            assert wait_done(browser, receipt["job_id"])["status"] == "done"
            assert seen == [expected]  # Neither corrupt nor duplicate normalization.
            assert tokens == ["once"]
            assert len(browser.app.state.manager.jobs) == 1
            assert not list(browser.app.state.audio_uploads.root.iterdir())
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_cancel_after_job_acceptance_uses_owner_and_stops_job(browser, monkeypatch):
    def pipeline(job, config):
        job.cancel_event.wait(3)
        if job.cancel_event.is_set():
            raise api.runproc.Cancelled()
        output = job.dir / "out.mp4"
        output.write_bytes(b"finished")
        return output

    monkeypatch.setattr(api, "run_pipeline", pipeline)
    data = wav()
    assert begin(browser, data).status_code == 200
    assert put(browser, data).status_code == 200
    job_id = submit(browser).json()["id"]
    other = TestClient(browser.app)
    assert other.delete(f"/api/audio-uploads/{SID}?cancel_job=true").status_code == 404
    assert browser.delete(f"/api/audio-uploads/{SID}?cancel_job=true").status_code == 200
    assert wait_done(browser, job_id)["status"] == "canceled"


def test_cancellation_waiting_for_admission_stops_the_new_job(browser, monkeypatch):
    from threading import Event, get_ident

    entered, waiting, release = Event(), Event(), Event()
    admission_thread = None
    store = browser.app.state.audio_uploads
    original_lock = store.lock
    manager = browser.app.state.manager
    original_create = manager.create

    class ObservedLock:
        def __enter__(self):
            if entered.is_set() and get_ident() != admission_thread:
                waiting.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    def create(*args, **kwargs):
        nonlocal admission_thread
        admission_thread = get_ident()
        entered.set()
        assert release.wait(5)
        return original_create(*args, **kwargs)

    def pipeline(job, config):
        job.cancel_event.wait(5)
        raise api.runproc.Cancelled()

    monkeypatch.setattr(store, "lock", ObservedLock())
    monkeypatch.setattr(manager, "create", create)
    monkeypatch.setattr(api, "run_pipeline", pipeline)
    data = wav()
    assert begin(browser, data).status_code == 200
    assert put(browser, data).status_code == 200
    with ThreadPoolExecutor(2) as executor:
        submission = executor.submit(submit, browser)
        try:
            assert entered.wait(5)
            cancellation = executor.submit(
                browser.delete, f"/api/audio-uploads/{SID}?cancel_job=true",
            )
            assert waiting.wait(5)
        finally:
            release.set()
        job_id = submission.result(timeout=5).json()["id"]
        assert cancellation.result(timeout=5).status_code == 200
    assert wait_done(browser, job_id)["status"] == "canceled"
