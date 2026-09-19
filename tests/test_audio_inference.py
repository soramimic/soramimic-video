import threading
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video.audio_inference import (
    InferenceScheduler,
    create_audio_inference_app,
    service_available,
)
from soramimic_video.transcribe import TranscribedLine


def _queued_job(scheduler, tmp_path, name, priority, kind="whisper"):
    work = tmp_path / "state/jobs" / name
    work.mkdir(parents=True)
    audio = work / "input.audio"
    audio.write_bytes(b"audio")
    return scheduler.add(kind, priority, {}, audio, work)


def test_service_readiness_allows_staggered_demucs_rollout(monkeypatch):
    class Response:
        ok = True

        @staticmethod
        def json():
            return {
                "status": "ok",
                "capabilities": {"whisper": True, "sheetsage2": True},
            }

    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: Response())

    assert service_available()


def test_service_readiness_rejects_incompatible_inference_api(monkeypatch):
    class Response:
        ok = True

        @staticmethod
        def json():
            return {
                "status": "ok",
                "api": {"name": "soramimic-audio-inference", "version": 2},
                "capabilities": {"whisper": True, "sheetsage2": True},
            }

    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: Response())

    assert not service_available()


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


def test_scheduler_overlaps_python_models_but_runs_demucs_exclusively(
    monkeypatch, tmp_path
):
    scheduler = InferenceScheduler(tmp_path / "state", device="cuda")
    monkeypatch.setattr(
        scheduler._gpu_admission,
        "_free_bytes",
        lambda _device: 12 * 1024**3,
    )
    active = set()
    lock = threading.Lock()
    python_models_started = threading.Event()

    def run(job):
        assert job.cuda_capacity_reserved
        with lock:
            if job.kind == "demucs":
                assert not active
            else:
                assert "demucs" not in active
            active.add(job.kind)
            if {"whisper", "sheetsage"}.issubset(active):
                python_models_started.set()
        if job.kind == "demucs":
            time.sleep(0.03)
        else:
            assert python_models_started.wait(2), "Python model workers did not overlap"
        with lock:
            active.remove(job.kind)
        return {}

    monkeypatch.setattr(scheduler, "_run", run)
    jobs = [
        _queued_job(scheduler, tmp_path, f"{kind}-job", "dev", kind)
        for kind in ("demucs", "whisper", "sheetsage")
    ]
    scheduler.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(job.status != "done" for job in jobs):
        time.sleep(0.01)
    scheduler.stop()

    assert all(job.status == "done" for job in jobs)


def test_demucs_releases_idle_model_caches_before_start(monkeypatch, tmp_path):
    from soramimic_video import audio_melody, kana_whisper, separation, transcribe

    scheduler = InferenceScheduler(tmp_path / "state", device="cpu")
    job = _queued_job(scheduler, tmp_path, "demucs-job", "dev", "demucs")
    transcribe._WHISPER_MODEL_CACHE[("large-v3", "cuda", None)] = object()
    kana_whisper._MODEL_CACHE[("cuda", "float16")] = object()
    audio_melody._MODEL_CACHE[("sheetsage",)] = object()

    def separate_local(_path, _output_dir, *, model, device):
        assert model == "htdemucs"
        assert device == "cpu"
        assert not transcribe._WHISPER_MODEL_CACHE
        assert not kana_whisper._MODEL_CACHE
        assert not audio_melody._MODEL_CACHE

    monkeypatch.setattr(separation, "_separate_local", separate_local)

    assert scheduler._run_demucs(job) == {
        "artifacts": ["no_vocals.wav", "vocals.wav"]
    }


def test_scheduler_rechecks_free_gpu_memory_and_releases_idle_caches(
    monkeypatch, tmp_path
):
    scheduler = InferenceScheduler(tmp_path / "state", device="cuda")
    free_bytes = [12 * 1024**3]
    releases = []

    monkeypatch.setattr(
        scheduler._gpu_admission,
        "_free_bytes",
        lambda _device: free_bytes[0],
    )

    def release_idle():
        releases.append(True)
        free_bytes[0] = 8 * 1024**3

    scheduler._gpu_admission._release_idle = release_idle
    first = _queued_job(scheduler, tmp_path, "first", "dev", "whisper")
    second = _queued_job(scheduler, tmp_path, "second", "dev", "sheetsage")

    with scheduler._gpu_admission.acquire(first, "cuda", scheduler._stop) as reserved:
        assert reserved
    free_bytes[0] = 3 * 1024**3
    with scheduler._gpu_admission.acquire(second, "cuda", scheduler._stop) as reserved:
        assert reserved

    assert releases == [True]


def test_auto_device_falls_back_to_cpu_when_gpu_capacity_is_not_reserved(tmp_path):
    scheduler = InferenceScheduler(tmp_path / "state", device="cuda")
    automatic = _queued_job(scheduler, tmp_path, "automatic", "dev", "sheetsage")
    automatic.parameters["device"] = "auto"
    explicit = _queued_job(scheduler, tmp_path, "explicit", "dev", "sheetsage")
    explicit.parameters["device"] = "cuda"

    assert scheduler._admission_device(automatic) == "cuda"
    assert scheduler._job_device(automatic) == "cpu"
    automatic.cuda_capacity_reserved = True
    assert scheduler._job_device(automatic) == "cuda"
    assert scheduler._job_device(explicit) == "cuda"


def test_scheduler_serializes_distinct_models_when_gpu_budget_is_low(
    monkeypatch, tmp_path
):
    scheduler = InferenceScheduler(tmp_path / "state", device="cuda")
    monkeypatch.setattr(
        scheduler._gpu_admission,
        "_free_bytes",
        lambda _device: 1024**3,
    )
    active = 0
    max_active = 0
    capacity_reservations = []
    lock = threading.Lock()

    def run(job):
        nonlocal active, max_active
        capacity_reservations.append(job.cuda_capacity_reserved)
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {}

    monkeypatch.setattr(scheduler, "_run", run)
    jobs = [
        _queued_job(scheduler, tmp_path, f"{kind}-job", "dev", kind)
        for kind in ("demucs", "whisper", "sheetsage")
    ]
    scheduler.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(job.status != "done" for job in jobs):
        time.sleep(0.01)
    scheduler.stop()

    assert all(job.status == "done" for job in jobs)
    assert max_active == 1
    assert capacity_reservations == [False, False, False]


def test_inference_api_runs_whisper_and_removes_consumed_job(monkeypatch, tmp_path):
    from soramimic_video import transcribe

    calls = []

    def transcribe_local(path, model_size, device, **kwargs):
        calls.append(
            (
                path.read_bytes(),
                model_size,
                device,
                kwargs["language"],
                kwargs["cache_model"],
                kwargs["cuda_capacity_reserved"],
            )
        )
        return [TranscribedLine(0.1, 0.8, "歌詞")]

    monkeypatch.setattr(transcribe, "_transcribe_lines_local", transcribe_local)
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.json()["api"] == {
            "name": "soramimic-audio-inference",
            "version": 1,
        }
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
                "parameters": (
                    '{"model_size":"large-v3","device":"auto","language":"en"}'
                ),
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
            "requested_language": "en",
            "lines": [{"start_sec": 0.1, "end_sec": 0.8, "text": "歌詞"}]
        }
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404

    assert calls == [(b"wave", "large-v3", "cpu", "en", True, False)]


@pytest.mark.parametrize(
    ("parameters", "expected_language"),
    [({}, "ja"), ({"language": None}, None)],
)
def test_whisper_worker_defaults_to_japanese_and_allows_detection(
    monkeypatch, tmp_path, parameters, expected_language
):
    from soramimic_video import transcribe

    observed = []

    def transcribe_local(_path, _model_size, _device, **kwargs):
        observed.append(kwargs["language"])
        return []

    monkeypatch.setattr(transcribe, "_transcribe_lines_local", transcribe_local)
    scheduler = InferenceScheduler(tmp_path / "state", device="cpu")
    job = _queued_job(scheduler, tmp_path, "whisper-language", "dev")
    job.parameters.update(parameters)

    assert scheduler._run(job) == {
        "requested_language": expected_language,
        "lines": [],
    }
    assert observed == [expected_language]


def test_inference_api_runs_kana_whisper_windows(monkeypatch, tmp_path):
    from soramimic_video import kana_whisper

    calls = []

    def transcribe_local(path, windows, device, **kwargs):
        calls.append((path.read_bytes(), windows, device, kwargs["cancel_check"]))
        return ["ナニオシテイタノ", "ナニオミテイタノ"]

    monkeypatch.setattr(kana_whisper, "_transcribe_kana_windows_local", transcribe_local)
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave", "audio/wav")},
            data={
                "kind": "kana-whisper",
                "priority": "dev",
                "parameters": '{"device":"auto","windows":[[1,4],[5,9]]}',
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
        assert response.json()["result"] == {"texts": ["ナニオシテイタノ", "ナニオミテイタノ"]}
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204

    assert calls[0][:3] == (b"wave", [(1.0, 4.0), (5.0, 9.0)], "cpu")
    assert callable(calls[0][3])



def test_inference_api_runs_demucs_and_serves_stems(monkeypatch, tmp_path):
    from soramimic_video import separation

    calls = []

    def separate_local(path, output_dir, *, model, device):
        calls.append((path.read_bytes(), model, device))
        output_dir.mkdir(parents=True)
        (output_dir / "vocals.wav").write_bytes(b"vocals")
        (output_dir / "no_vocals.wav").write_bytes(b"accompaniment")

    monkeypatch.setattr(separation, "_separate_local", separate_local)
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave", "audio/wav")},
            data={
                "kind": "demucs",
                "priority": "public",
                "parameters": '{"model":"htdemucs","device":"auto"}',
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = client.get(f"/v1/jobs/{job_id}")
            if response.json()["status"] == "done":
                break
            time.sleep(0.01)

        assert response.json()["status"] == "done"
        assert response.json()["result"] == {
            "artifacts": ["no_vocals.wav", "vocals.wav"]
        }
        vocals = client.get(f"/v1/jobs/{job_id}/artifacts/vocals.wav")
        accompaniment = client.get(
            f"/v1/jobs/{job_id}/artifacts/no_vocals.wav"
        )
        assert vocals.content == b"vocals"
        assert accompaniment.content == b"accompaniment"
        assert client.get(
            f"/v1/jobs/{job_id}/artifacts/../input.audio"
        ).status_code == 404
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204

    assert calls == [(b"wave", "htdemucs", "cpu")]


def test_running_demucs_is_killed_when_cancelled(monkeypatch, tmp_path):
    from soramimic_video import runproc, separation

    started = threading.Event()
    killed = threading.Event()

    def separate_local(_path, _output_dir, *, model, device):
        started.set()
        assert killed.wait(2)

    monkeypatch.setattr(separation, "_separate_local", separate_local)
    monkeypatch.setattr(runproc, "kill_current", lambda: killed.set() or True)
    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave")},
            data={"kind": "demucs", "priority": "dev", "parameters": "{}"},
        )
        job_id = submitted.json()["id"]
        assert started.wait(2)
        assert client.delete(f"/v1/jobs/{job_id}").status_code == 204
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if client.get(f"/v1/jobs/{job_id}").status_code == 404:
                break
            time.sleep(0.01)

        assert killed.is_set()
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404


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


@pytest.mark.parametrize("language", ["", "EN", "english", 1, True, [], {}])
def test_inference_api_rejects_invalid_whisper_language(tmp_path, language):
    import json

    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave")},
            data={
                "kind": "whisper",
                "priority": "dev",
                "parameters": json.dumps({"language": language}),
            },
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "windows",
    [[], [[-1, 2]], [[2, 1]], [[0, 25]], [[2, 3], [1, 2]], [[0, "later"]]],
)
def test_inference_api_rejects_invalid_kana_windows(tmp_path, windows):
    import json

    app = create_audio_inference_app(tmp_path / "state", device="cpu")
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            files={"audio": ("song.wav", b"wave")},
            data={
                "kind": "kana-whisper",
                "priority": "dev",
                "parameters": json.dumps({"windows": windows}),
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
        language=None,
        vad_filter=False,
        condition_on_previous_text=False,
    )

    assert [line.text for line in result] == ["共有"]
    assert calls == [
        (
            (audio, "large-v3", "auto"),
            {
                "language": None,
                "vad_filter": False,
                "condition_on_previous_text": False,
            },
        )
    ]


def test_remote_whisper_sends_language_and_checks_server_support(monkeypatch, tmp_path):
    from soramimic_video import audio_inference

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    calls = []

    def infer(kind, path, parameters):
        calls.append((kind, path, parameters))
        return {"requested_language": "en", "lines": []}

    monkeypatch.setattr(audio_inference, "_remote_inference", infer)

    assert audio_inference.transcribe_lines_remote(
        audio,
        "large-v3",
        "auto",
        language="en",
        vad_filter=False,
        condition_on_previous_text=False,
    ) == []
    assert calls == [
        (
            "whisper",
            audio,
            {
                "model_size": "large-v3",
                "device": "auto",
                "language": "en",
                "vad_filter": False,
                "condition_on_previous_text": False,
            },
        )
    ]


def test_remote_whisper_rejects_legacy_server_for_nondefault_language(
    monkeypatch, tmp_path
):
    from soramimic_video import audio_inference

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    monkeypatch.setattr(
        audio_inference,
        "_remote_inference",
        lambda *_args, **_kwargs: {"lines": []},
    )

    with pytest.raises(RuntimeError, match="言語指定に対応していません"):
        audio_inference.transcribe_lines_remote(
            audio,
            "large-v3",
            "auto",
            language=None,
            vad_filter=False,
            condition_on_previous_text=False,
        )


def test_kana_whisper_delegates_to_configured_shared_service(monkeypatch, tmp_path):
    from soramimic_video import audio_inference, kana_whisper

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    calls = []
    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")
    monkeypatch.setattr(
        audio_inference,
        "transcribe_kana_windows_remote",
        lambda *args: calls.append(args) or ["カナ"],
    )

    assert kana_whisper.transcribe_kana_windows(audio, [(1.0, 2.0)], "auto") == ["カナ"]
    assert calls == [(audio, [(1.0, 2.0)], "auto")]



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


def test_separation_delegates_to_configured_shared_service(monkeypatch, tmp_path):
    from soramimic_video import audio_inference, separation

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"wave")
    output = tmp_path / "separation"
    calls = []
    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")

    def separate_remote(audio_path, out_dir, *, model, device):
        calls.append((audio_path, out_dir, model, device))
        out_dir.mkdir()
        vocals = out_dir / "vocals.wav"
        accompaniment = out_dir / "no_vocals.wav"
        vocals.write_bytes(b"vocals")
        accompaniment.write_bytes(b"accompaniment")
        return vocals, accompaniment

    monkeypatch.setattr(audio_inference, "separate_remote", separate_remote)

    vocals, accompaniment = separation.separate(audio, output)

    assert vocals.read_bytes() == b"vocals"
    assert accompaniment.read_bytes() == b"accompaniment"
    assert calls == [(audio, output, "htdemucs", "auto")]
