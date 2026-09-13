import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video.audio_inference import (
    InferenceScheduler,
    create_audio_inference_app,
)
from soramimic_video.transcribe import TranscribedLine


def _queued_job(scheduler, tmp_path, name, priority):
    work = tmp_path / "state/jobs" / name
    work.mkdir(parents=True)
    audio = work / "input.audio"
    audio.write_bytes(b"audio")
    return scheduler.add("whisper", priority, {}, audio, work)


def test_scheduler_orders_queued_work_by_environment_priority(monkeypatch, tmp_path):
    scheduler = InferenceScheduler(tmp_path / "state", device="cpu")
    order = []

    def run(job):
        order.append(job.priority)
        return {"lines": []}

    monkeypatch.setattr(scheduler, "_run", run)
    dev = _queued_job(scheduler, tmp_path, "dev-job", "dev")
    _queued_job(scheduler, tmp_path, "public-job", "public")
    scheduler.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and dev.status != "done":
        time.sleep(0.01)
    scheduler.stop()

    assert order == ["public", "dev"]


def test_inference_api_runs_whisper_and_removes_consumed_job(monkeypatch, tmp_path):
    from soramimic_video import transcribe

    calls = []

    def transcribe_local(path, model_size, device, **kwargs):
        calls.append((path.read_bytes(), model_size, device, kwargs["cache_model"]))
        return [TranscribedLine(0.1, 0.8, "歌詞")]

    monkeypatch.setattr(transcribe, "_transcribe_lines_local", transcribe_local)
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.json()["jobs"] == {
            "queued": 0,
            "running": 0,
            "done": 0,
            "error": 0,
            "cancelled": 0,
        }
        submitted = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave", "audio/wav")},
            data={
                "kind": "whisper",
                "priority": "preview",
                "parameters": '{"model_size":"large-v3","device":"auto"}',
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = client.get(f"/v1/jobs/{job_id}")
            if response.json()["status"] == "done":
                break
            time.sleep(0.01)

        assert response.json()["result"] == {
            "lines": [{"start_sec": 0.1, "end_sec": 0.8, "text": "歌詞"}]
        }
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404

    assert calls == [(b"wave", "large-v3", "cpu", True)]


def test_inference_api_rejects_invalid_priority(tmp_path):
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave")},
            data={"kind": "whisper", "priority": "root", "parameters": "{}"},
        )
    assert response.status_code == 422


def test_inference_api_rejects_unconfigured_model(tmp_path):
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave")},
            data={
                "kind": "whisper",
                "priority": "dev",
                "parameters": '{"model_size":"untrusted/model"}',
            },
        )
    assert response.status_code == 422


def test_transcribe_delegates_to_configured_shared_service(monkeypatch, tmp_path):
    from soramimic_video import audio_inference, transcribe

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    calls = []
    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320/")
    monkeypatch.setattr(
        audio_inference,
        "transcribe_lines_remote",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or [TranscribedLine(0.0, 1.0, "共有")],
    )

    result = transcribe.transcribe_lines(
        audio,
        "large-v3",
        "auto",
        vad_filter=False,
        condition_on_previous_text=False,
    )

    assert [line.text for line in result] == ["共有"]
    assert calls == [
        (
            (audio, "large-v3", "auto"),
            {"vad_filter": False, "condition_on_previous_text": False},
        )
    ]


def test_sheetsage_delegates_and_reports_shared_capability(monkeypatch, tmp_path):
    from soramimic_video import audio_inference, audio_melody

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    progress = []
    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")
    monkeypatch.setattr(
        audio_inference,
        "transcribe_sheetsage_remote",
        lambda *args, **kwargs: kwargs["on_progress"](1.0) or [],
    )

    assert audio_melody.configured_capabilities()["sheetsage2"] is True
    assert audio_melody.transcribe_sheetsage(
        audio,
        tmp_path / "unused",
        device="auto",
        on_progress=progress.append,
    ) == []
    assert progress == [1.0]
