"""Generation backgrounds survive page reloads and follow job access/retention rules."""

from __future__ import annotations

import io
import threading
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from PIL.PngImagePlugin import PngInfo  # noqa: E402

from soramimic_video import api  # noqa: E402


def png(color="red", size=(640, 360)):
    output = io.BytesIO()
    info = PngInfo()
    info.add_text("private", "discard this metadata")
    Image.new("RGB", size, color).save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def submit(client, thumbnail):
    return client.post("/api/jobs", data={"wordlist": "stations"}, files={
        "midi": ("song.mid", b"MThd" + b"\0" * 16, "audio/midi"),
        "generation_thumbnail": ("../../preview.png", thumbnail, "image/png"),
    })


@pytest.mark.parametrize("outcome", ["done", "error", "canceled"])
def test_generation_background_lifecycle(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    monkeypatch.setenv(api.API_KEY_ENV, "thumbnail-key")
    monkeypatch.setattr(api, "song_seconds", lambda _: 0)
    release = threading.Event()
    started = threading.Event()
    final_thumbnail = png("blue")

    def pipeline(job, config):
        started.set()
        assert release.wait(10)
        if outcome == "error":
            raise RuntimeError("test failure")
        (job.dir / "thumbnail.png").write_bytes(final_thumbnail)
        video = job.dir / "song.mp4"
        video.write_bytes(b"video")
        return video

    monkeypatch.setattr(api, "run_pipeline", pipeline)
    app = api.create_app(jobs_dir=tmp_path / "jobs")
    headers = {"X-API-Key": "thumbnail-key"}
    alice = TestClient(app, base_url="https://testserver", headers=headers)
    bob = TestClient(app, base_url="https://testserver", headers=headers)
    try:
        response = submit(alice, png())
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        assert started.wait(5)
        job = app.state.manager.jobs[job_id]
        assert job.generation_thumbnail.is_file()

        # A fresh page uses only the server-side job list and its session cookie.
        reloaded = TestClient(app, base_url="https://testserver", headers=headers,
                              cookies=alice.cookies)
        body = reloaded.get("/api/jobs").json()[0]
        assert body["id"] == job_id
        assert body["status"] == "running"
        url = body["thumbnail_url"]
        assert reloaded.get(f"/api/jobs/{job_id}").json()["thumbnail_url"] == url
        image_response = reloaded.get(url)
        assert image_response.status_code == 200
        assert image_response.headers["cache-control"] == "private, no-store"
        with Image.open(io.BytesIO(image_response.content)) as image:
            assert image.size == (640, 360)
            assert image.getpixel((0, 0)) == (255, 0, 0, 255)
            assert "private" not in image.info
        assert "thumbnail" not in job.usage_outputs_recorded
        assert bob.get(url).status_code == 404
        assert TestClient(app).get(url).status_code == 401

        # A second job waits behind the first and already has its background.
        queued_response = submit(alice, png("green"))
        assert queued_response.status_code == 200, queued_response.text
        queued_id = queued_response.json()["id"]
        queued = reloaded.get(f"/api/jobs/{queued_id}").json()
        assert queued["status"] == "queued"
        assert reloaded.get(queued["thumbnail_url"]).status_code == 200
        assert reloaded.post(f"/api/jobs/{queued_id}/cancel").status_code == 200
        assert not app.state.manager.jobs[queued_id].generation_thumbnail.exists()

        if outcome == "canceled":
            assert alice.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        release.set()
        for _ in range(200):
            body = reloaded.get(f"/api/jobs/{job_id}").json()
            if body["status"] in {"done", "error", "canceled"}:
                break
            time.sleep(0.02)
        assert body["status"] == outcome
        assert not job.generation_thumbnail.exists()
        if outcome == "done":
            assert reloaded.get(body["thumbnail_url"]).content == final_thumbnail
        else:
            assert "thumbnail_url" not in body
            assert reloaded.get(url).status_code == 404
    finally:
        release.set()


@pytest.mark.parametrize("data,status", [
    (b"not an image", 422),
    (png(size=(1281, 360)), 422),
    (png(size=(640, 721)), 422),
    (b"x" * (api.MAX_GENERATION_THUMBNAIL_BYTES + 1), 413),
])
def test_invalid_generation_background_is_rejected(tmp_path, monkeypatch, data, status):
    monkeypatch.setattr(api, "run_pipeline", lambda *_: pytest.fail("must not start"))
    app = api.create_app(jobs_dir=tmp_path / "jobs")
    response = submit(TestClient(app), data)
    assert response.status_code == status, response.text
    assert not app.state.manager.jobs
