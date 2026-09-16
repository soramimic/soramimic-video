"""Shared, loopback-only audio-model inference service.

Application environments submit short-lived jobs to one process so heavyweight
models are loaded only once.  One worker per model family allows Demucs, Whisper,
and SheetSage to overlap when the measured GPU budget is large enough, while work
within each family remains priority ordered.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import logging
import math
import os
import queue
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

AUDIO_INFERENCE_URL_ENV = "SORAMIMIC_AUDIO_INFERENCE_URL"
AUDIO_INFERENCE_PRIORITY_ENV = "SORAMIMIC_AUDIO_INFERENCE_PRIORITY"
AUDIO_INFERENCE_MAX_UPLOAD_BYTES_ENV = "SORAMIMIC_AUDIO_INFERENCE_MAX_UPLOAD_BYTES"
AUDIO_INFERENCE_WHISPER_MODELS_ENV = "SORAMIMIC_AUDIO_INFERENCE_WHISPER_MODELS"

DEFAULT_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
PRIORITIES = {"public": 0, "preview": 10, "dev": 20, "eval": 30}
POLL_SECONDS = 0.5
ALLOWED_DEVICES = {"auto", "cpu", "cuda", "cuda:0"}
DEMUCS_ARTIFACTS = {"vocals.wav", "no_vocals.wav"}
INFERENCE_KINDS = ("demucs", "whisper", "kana-whisper", "sheetsage")
_GPU_HEADROOM_BYTES = 1024**3
_GPU_RESERVE_BYTES = {
    "demucs": 2 * 1024**3,
    "whisper": 3 * 1024**3,
    "kana-whisper": 3 * 1024**3,
    "sheetsage": 3 * 1024**3,
}


class InferenceCancelled(Exception):  # noqa: N818 - internal control flow
    """The caller cancelled a queued or running inference request."""


class _GpuAdmission:
    """Reserve GPU capacity while keeping Demucs isolated from resident models."""

    def __init__(self, release_idle: Callable[[], None] | None = None) -> None:
        self._condition = threading.Condition()
        self._active: dict[str, tuple[str, int]] = {}
        self._exclusive_waiters = 0
        self._release_idle = release_idle

    @staticmethod
    def _free_bytes(device: str) -> int | None:
        try:
            import torch

            free_bytes, _ = torch.cuda.mem_get_info(device)
        except (ImportError, RuntimeError):
            return None
        return int(free_bytes)

    @contextmanager
    def acquire(
        self,
        job: InferenceJob,
        device: str,
        stop: threading.Event,
    ):
        uses_cuda = device.startswith("cuda")
        reservation = _GPU_RESERVE_BYTES[job.kind] if uses_cuda else 0
        capacity_reserved = False
        with self._condition:
            exclusive = job.kind == "demucs"
            if exclusive:
                self._exclusive_waiters += 1
            try:
                while True:
                    if stop.is_set() or job.cancel_event.is_set():
                        raise InferenceCancelled()
                    demucs_active = any(
                        kind == "demucs" for kind, _reserved in self._active.values()
                    )
                    if exclusive:
                        admitted = not self._active
                    else:
                        # Once Demucs is waiting, drain the current wave instead of
                        # allowing a steady stream of model jobs to starve it.
                        admitted = not demucs_active and self._exclusive_waiters == 0
                    if not admitted:
                        self._condition.wait(timeout=0.1)
                        continue
                    if not uses_cuda:
                        self._active[job.id] = (job.kind, reservation)
                        break
                    active_reservations = sum(
                        value for _kind, value in self._active.values()
                    )
                    free_bytes = self._free_bytes(device)
                    available = max(
                        0,
                        (free_bytes or 0)
                        - _GPU_HEADROOM_BYTES
                        - active_reservations,
                    )
                    if not self._active:
                        if reservation > available and self._release_idle is not None:
                            logger.info(
                                "GPU空き容量が不足しているため待機中のモデルキャッシュを解放します"
                            )
                            self._release_idle()
                            free_bytes = self._free_bytes(device)
                            available = max(
                                0,
                                (free_bytes or 0) - _GPU_HEADROOM_BYTES,
                            )
                        self._active[job.id] = (job.kind, reservation)
                        capacity_reserved = reservation <= available
                        break
                    if reservation <= available:
                        self._active[job.id] = (job.kind, reservation)
                        capacity_reserved = True
                        break
                    self._condition.wait(timeout=0.1)
            finally:
                if exclusive:
                    self._exclusive_waiters -= 1
        try:
            yield capacity_reserved
        finally:
            with self._condition:
                self._active.pop(job.id, None)
                self._condition.notify_all()


def configured_url() -> str | None:
    value = os.environ.get(AUDIO_INFERENCE_URL_ENV, "").strip()
    return value.rstrip("/") or None


def configured_priority() -> str:
    value = os.environ.get(AUDIO_INFERENCE_PRIORITY_ENV, "dev").strip().lower()
    return value if value in PRIORITIES else "dev"


def service_available(timeout: float = 2.0) -> bool:
    base_url = configured_url()
    if base_url is None:
        return False
    try:
        response = requests.get(f"{base_url}/healthz", timeout=(timeout, timeout))
        body = response.json()
    except (requests.RequestException, ValueError):
        return False
    capabilities = body.get("capabilities", {})
    return bool(
        response.ok
        and body.get("status") == "ok"
        and capabilities.get("whisper")
        and capabilities.get("sheetsage2")
    )


def _raise_response(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if detail:
            raise RuntimeError(f"共有音声推論サービス: {detail}") from exc
        raise RuntimeError(f"共有音声推論サービス: HTTP {response.status_code}") from exc


def _remote_inference(
    kind: str,
    audio_path: Path,
    parameters: dict[str, Any],
    *,
    on_progress: Callable[[float], None] | None = None,
    artifact_targets: dict[str, Path] | None = None,
) -> Any:
    """Submit, poll, and clean up one remote inference job."""
    from . import runproc

    base_url = configured_url()
    if base_url is None:
        raise RuntimeError("共有音声推論サービスURLが設定されていません")
    job_id: str | None = None
    try:
        with audio_path.open("rb") as audio:
            response = requests.post(
                f"{base_url}/v1/jobs",
                files={"audio": (audio_path.name, audio, "application/octet-stream")},
                data={
                    "kind": kind,
                    "priority": configured_priority(),
                    "parameters": json.dumps(parameters, ensure_ascii=False),
                },
                timeout=(5, 300),
            )
        _raise_response(response)
        job_id = str(response.json()["id"])
        while True:
            runproc.raise_if_cancelled()
            response = requests.get(f"{base_url}/v1/jobs/{job_id}", timeout=(5, 30))
            _raise_response(response)
            body = response.json()
            status = body.get("status")
            if on_progress is not None:
                on_progress(float(body.get("progress") or 0.0))
            if status == "done":
                if artifact_targets:
                    _download_artifacts(base_url, job_id, artifact_targets)
                return body.get("result")
            if status == "error":
                raise RuntimeError(
                    f"共有音声推論サービスで{kind}が失敗しました: "
                    f"{body.get('error') or '詳細不明'}"
                )
            if status == "cancelled":
                raise runproc.Cancelled()
            time.sleep(POLL_SECONDS)
    finally:
        if job_id is not None:
            try:
                requests.delete(f"{base_url}/v1/jobs/{job_id}", timeout=(5, 10))
            except requests.RequestException:
                logger.warning("共有音声推論ジョブ%sの後片付け要求に失敗しました", job_id)


def _download_artifacts(
    base_url: str,
    job_id: str,
    targets: dict[str, Path],
) -> None:
    """Download a completed job's artifacts, publishing all files atomically."""
    from . import runproc

    temporary: dict[Path, Path] = {}
    try:
        for artifact_name, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary[target] = temp
            with requests.get(
                f"{base_url}/v1/jobs/{job_id}/artifacts/{artifact_name}",
                stream=True,
                timeout=(5, 300),
            ) as response:
                _raise_response(response)
                with temp.open("xb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        runproc.raise_if_cancelled()
                        if chunk:
                            output.write(chunk)
        for target, temp in temporary.items():
            temp.replace(target)
    finally:
        for temp in temporary.values():
            temp.unlink(missing_ok=True)


def separate_remote(
    audio_path: Path,
    out_dir: Path,
    *,
    model: str,
    device: str,
) -> tuple[Path, Path]:
    """Run Demucs through the shared scheduler and download both stems."""
    vocals = out_dir / "vocals.wav"
    accompaniment = out_dir / "no_vocals.wav"
    result = _remote_inference(
        "demucs",
        audio_path,
        {"model": model, "device": device},
        artifact_targets={
            "vocals.wav": vocals,
            "no_vocals.wav": accompaniment,
        },
    )
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    expected = {"vocals.wav", "no_vocals.wav"}
    if not isinstance(artifacts, list) or set(artifacts) != expected:
        vocals.unlink(missing_ok=True)
        accompaniment.unlink(missing_ok=True)
        raise RuntimeError("共有Demucsの応答形式が不正です")
    return vocals, accompaniment


def transcribe_lines_remote(
    audio_path: Path,
    model_size: str,
    device: str,
    *,
    vad_filter: bool,
    condition_on_previous_text: bool,
):
    from .transcribe import TranscribedLine

    result = _remote_inference(
        "whisper",
        audio_path,
        {
            "model_size": model_size,
            "device": device,
            "vad_filter": vad_filter,
            "condition_on_previous_text": condition_on_previous_text,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("lines"), list):
        raise RuntimeError("共有Whisperの応答形式が不正です")
    return [
        TranscribedLine(
            start_sec=float(line["start_sec"]),
            end_sec=float(line["end_sec"]),
            text=str(line["text"]),
        )
        for line in result["lines"]
    ]


def transcribe_kana_windows_remote(
    audio_path: Path,
    windows: Sequence[tuple[float, float]],
    device: str,
) -> list[str]:
    result = _remote_inference(
        "kana-whisper",
        audio_path,
        {
            "device": device,
            "windows": [[start, end] for start, end in windows],
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("texts"), list):
        raise RuntimeError("共有KanaWhisperの応答形式が不正です")
    texts = result["texts"]
    if len(texts) != len(windows) or not all(isinstance(text, str) for text in texts):
        raise RuntimeError("共有KanaWhisperの応答件数が不正です")
    return texts


def transcribe_sheetsage_remote(
    audio_path: Path,
    device: str,
    *,
    on_progress: Callable[[float], None] | None = None,
):
    from .audio_melody import MelodyNote

    result = _remote_inference(
        "sheetsage",
        audio_path,
        {"device": device},
        on_progress=on_progress,
    )
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("notes"), list):
        raise RuntimeError("共有SheetSage2の応答形式が不正です")
    return [
        MelodyNote(
            start_sec=float(note["start_sec"]),
            end_sec=float(note["end_sec"]),
            midi_note=int(note["midi_note"]),
        )
        for note in result["notes"]
    ]


@dataclass
class InferenceJob:
    id: str
    kind: str
    priority: str
    parameters: dict[str, Any]
    audio_path: Path
    work_dir: Path
    status: str = "queued"
    result: Any = None
    error: str | None = None
    progress: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    remove_when_done: bool = False
    cuda_capacity_reserved: bool = False


class InferenceScheduler:
    """Priority scheduler with one capacity-gated worker per model family."""

    def __init__(self, state_dir: Path, *, device: str = "cuda") -> None:
        self.state_dir = state_dir
        self.device = device
        self._jobs: dict[str, InferenceJob] = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self._queues: dict[
            str, queue.PriorityQueue[tuple[int, int, str]]
        ] = {kind: queue.PriorityQueue() for kind in INFERENCE_KINDS}
        self._stop = threading.Event()
        self._workers: dict[str, threading.Thread] = {}
        self._gpu_admission = _GpuAdmission(self._release_idle_model_caches)

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self._workers and all(worker.is_alive() for worker in self._workers.values()):
            return
        if not self._jobs:
            shutil.rmtree(self.state_dir / "jobs", ignore_errors=True)
        (self.state_dir / "jobs").mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._workers = {
            kind: threading.Thread(
                target=self._loop,
                args=(kind,),
                name=f"audio-inference-{kind}",
                daemon=True,
            )
            for kind in INFERENCE_KINDS
        }
        for worker in self._workers.values():
            worker.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for job in self._jobs.values():
                if job.status in {"queued", "running"}:
                    job.cancel_event.set()
        for pending in self._queues.values():
            pending.put((-1, -1, ""))
        for worker in self._workers.values():
            worker.join(timeout=30)
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            shutil.rmtree(job.work_dir, ignore_errors=True)

    def add(
        self,
        kind: str,
        priority: str,
        parameters: dict[str, Any],
        audio_path: Path,
        work_dir: Path,
    ) -> InferenceJob:
        if kind not in INFERENCE_KINDS:
            raise ValueError(
                "kindはdemucs、whisper、kana-whisper、sheetsageのいずれかです"
            )
        if priority not in PRIORITIES:
            raise ValueError("priorityはpublic、preview、dev、evalのいずれかです")
        job = InferenceJob(
            id=work_dir.name,
            kind=kind,
            priority=priority,
            parameters=parameters,
            audio_path=audio_path,
            work_dir=work_dir,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._sequence += 1
            sequence = self._sequence
        self._queues[kind].put((PRIORITIES[priority], sequence, job.id))
        return job

    def get(self, job_id: str) -> InferenceJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_and_remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
            elif job.status == "running":
                job.remove_when_done = True
            terminal = job.status in {"done", "error", "cancelled"}
            if terminal:
                self._jobs.pop(job_id, None)
        if terminal:
            shutil.rmtree(job.work_dir, ignore_errors=True)
        return True

    def snapshot(self, job: InferenceJob) -> dict[str, Any]:
        with self._lock:
            return {
                "id": job.id,
                "kind": job.kind,
                "priority": job.priority,
                "status": job.status,
                "progress": job.progress,
                "result": job.result if job.status == "done" else None,
                "error": job.error if job.status == "error" else None,
            }

    def healthy(self) -> bool:
        return bool(self._workers) and all(
            worker.is_alive() for worker in self._workers.values()
        )

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("queued", "running", "done", "error", "cancelled")}
        with self._lock:
            for job in self._jobs.values():
                if job.status in counts:
                    counts[job.status] += 1
        return counts

    def _check_cancelled(self, job: InferenceJob) -> None:
        if self._stop.is_set() or job.cancel_event.is_set():
            raise InferenceCancelled()

    def _progress(self, job: InferenceJob, value: float) -> None:
        self._check_cancelled(job)
        with self._lock:
            job.progress = max(job.progress, min(1.0, max(0.0, value)))

    def _admission_device(self, job: InferenceJob) -> str:
        requested = str(job.parameters.get("device") or self.device)
        return self.device if requested == "auto" else requested

    def _job_device(self, job: InferenceJob) -> str:
        requested = str(job.parameters.get("device") or self.device)
        device = self.device if requested == "auto" else requested
        if (
            requested == "auto"
            and device.startswith("cuda")
            and not job.cuda_capacity_reserved
        ):
            logger.warning(
                "GPU空き容量が不足しているため%sをCPUで実行します", job.kind
            )
            return "cpu"
        return device

    @staticmethod
    def _release_idle_model_caches() -> None:
        from . import audio_melody, kana_whisper, transcribe

        transcribe._WHISPER_MODEL_CACHE.clear()
        kana_whisper._MODEL_CACHE.clear()
        audio_melody._MODEL_CACHE.clear()
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, RuntimeError):
            pass

    def _run(self, job: InferenceJob) -> Any:
        self._check_cancelled(job)
        if job.kind == "demucs":
            return self._run_demucs(job)
        if job.kind == "whisper":
            from .transcribe import _transcribe_lines_local

            model_size = str(job.parameters.get("model_size") or "large-v3")
            device = self._job_device(job)
            lines = _transcribe_lines_local(
                job.audio_path,
                model_size,
                device,
                vad_filter=bool(job.parameters.get("vad_filter", True)),
                condition_on_previous_text=bool(
                    job.parameters.get("condition_on_previous_text", True)
                ),
                cache_model=True,
                cuda_capacity_reserved=job.cuda_capacity_reserved,
                cancel_check=lambda: self._check_cancelled(job),
            )
            return {
                "lines": [
                    {
                        "start_sec": line.start_sec,
                        "end_sec": line.end_sec,
                        "text": line.text,
                    }
                    for line in lines
                ]
            }

        if job.kind == "kana-whisper":
            from .kana_whisper import _transcribe_kana_windows_local

            raw_windows = job.parameters.get("windows")
            assert isinstance(raw_windows, list)
            windows = [(float(item[0]), float(item[1])) for item in raw_windows]
            return {
                "texts": _transcribe_kana_windows_local(
                    job.audio_path,
                    windows,
                    self._job_device(job),
                    cancel_check=lambda: self._check_cancelled(job),
                )
            }

        from .audio_melody import _transcribe_sheetsage_local

        device = self._job_device(job)
        notes = _transcribe_sheetsage_local(
            job.audio_path,
            job.work_dir / "output",
            device=device,
            on_progress=lambda value: self._progress(job, value),
        )
        if notes is None:
            return None
        return {
            "notes": [
                {
                    "start_sec": note.start_sec,
                    "end_sec": note.end_sec,
                    "midi_note": note.midi_note,
                }
                for note in notes
            ]
        }

    def _run_demucs(self, job: InferenceJob) -> dict[str, list[str]]:
        from . import runproc
        from .separation import DEMUCS_MODEL, _separate_local

        # Demucs runs in a separate CUDA process.  The admission gate guarantees
        # that no model invocation is active here, so cached model references can
        # be released without invalidating another worker.  Without this step,
        # successive song jobs retain Whisper, KanaWhisper, and SheetSage on the
        # GPU and can leave Demucs stalled behind several gigabytes of idle models.
        self._release_idle_model_caches()

        finished = threading.Event()

        def stop_subprocess_on_cancel() -> None:
            while not finished.wait(0.1):
                if self._stop.is_set() or job.cancel_event.is_set():
                    if runproc.kill_current():
                        return

        watcher = threading.Thread(
            target=stop_subprocess_on_cancel,
            name=f"demucs-cancel-{job.id}",
            daemon=True,
        )
        watcher.start()
        device = self._job_device(job)
        model = str(job.parameters.get("model") or DEMUCS_MODEL)
        try:
            try:
                _separate_local(
                    job.audio_path,
                    job.work_dir / "output",
                    model=model,
                    device=device,
                )
            except Exception:
                self._check_cancelled(job)
                raise
            self._check_cancelled(job)
        finally:
            finished.set()
            watcher.join(timeout=1)
        return {"artifacts": sorted(DEMUCS_ARTIFACTS)}

    def _loop(self, kind: str) -> None:
        while not self._stop.is_set():
            _priority, _sequence, job_id = self._queues[kind].get()
            if not job_id:
                continue
            job = self.get(job_id)
            if job is None or job.status != "queued":
                continue
            try:
                with self._gpu_admission.acquire(
                    job,
                    self._admission_device(job),
                    self._stop,
                ) as capacity_reserved:
                    job.cuda_capacity_reserved = capacity_reserved
                    with self._lock:
                        if job.cancel_event.is_set():
                            raise InferenceCancelled()
                        job.status = "running"
                    result = self._run(job)
            except InferenceCancelled:
                with self._lock:
                    job.status = "cancelled"
            except Exception as exc:  # noqa: BLE001 - isolate each submitted job
                logger.exception("共有%s推論が失敗しました", job.kind)
                with self._lock:
                    job.error = str(exc)
                    job.status = "error"
            else:
                with self._lock:
                    job.result = result
                    job.progress = 1.0
                    job.status = "done"
            finally:
                try:
                    job.audio_path.unlink(missing_ok=True)
                    if job.kind != "demucs" or job.status != "done":
                        shutil.rmtree(job.work_dir / "output", ignore_errors=True)
                except OSError:
                    logger.warning("共有音声推論ジョブ%sの一時ファイルを削除できません", job.id)
                if job.remove_when_done:
                    with self._lock:
                        self._jobs.pop(job.id, None)
                    shutil.rmtree(job.work_dir, ignore_errors=True)


def create_audio_inference_app(state_dir: Path, *, device: str = "cuda"):
    """Create the private FastAPI application used by the systemd service."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, File, Form, HTTPException
    from fastapi.responses import FileResponse

    scheduler = InferenceScheduler(state_dir, device=device)
    max_upload_bytes = int(
        os.environ.get(AUDIO_INFERENCE_MAX_UPLOAD_BYTES_ENV, DEFAULT_MAX_UPLOAD_BYTES)
    )
    allowed_models = {
        value.strip()
        for value in os.environ.get(
            AUDIO_INFERENCE_WHISPER_MODELS_ENV, "large-v3"
        ).split(",")
        if value.strip()
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(
        title="Soramimic shared audio inference",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        from .audio_melody import _configured_local_capabilities
        from .kana_whisper import model_available

        return {
            "status": "ok" if scheduler.healthy() else "starting",
            "jobs": scheduler.status_counts(),
            "capabilities": {
                "demucs": importlib.util.find_spec("demucs") is not None,
                "whisper": importlib.util.find_spec("faster_whisper") is not None,
                "kana_whisper": model_available(),
                "sheetsage2": _configured_local_capabilities()["sheetsage2"],
            },
        }

    @app.post("/v1/jobs", status_code=202)
    async def submit_job(
        audio: Any = File(...),
        kind: str = Form(...),
        priority: str = Form("dev"),
        parameters: str = Form("{}"),
    ) -> dict[str, str]:
        if kind not in INFERENCE_KINDS:
            raise HTTPException(
                422,
                "kindはdemucs、whisper、kana-whisper、sheetsageのいずれかです",
            )
        if priority not in PRIORITIES:
            raise HTTPException(422, "priorityが不正です")
        try:
            parsed = json.loads(parameters)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "parametersはJSON objectで指定してください") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(422, "parametersはJSON objectで指定してください")
        requested_device = str(parsed.get("device") or "auto")
        if requested_device not in ALLOWED_DEVICES:
            raise HTTPException(422, "deviceが不正です")
        if kind == "whisper":
            model_size = str(parsed.get("model_size") or "large-v3")
            if model_size not in allowed_models:
                raise HTTPException(422, "許可されていないWhisperモデルです")
            for option in ("vad_filter", "condition_on_previous_text"):
                if option in parsed and not isinstance(parsed[option], bool):
                    raise HTTPException(422, f"{option}はbooleanで指定してください")
        elif kind == "kana-whisper":
            from .kana_whisper import KANA_CONTEXT_MAX_SEC, KANA_MAX_WINDOWS

            windows = parsed.get("windows")
            if not isinstance(windows, list) or not 1 <= len(windows) <= KANA_MAX_WINDOWS:
                raise HTTPException(422, "KanaWhisperのwindowsが不正です")
            previous_start = -1.0
            for window in windows:
                if (
                    not isinstance(window, list)
                    or len(window) != 2
                    or isinstance(window[0], bool)
                    or isinstance(window[1], bool)
                ):
                    raise HTTPException(422, "KanaWhisperのwindowが不正です")
                try:
                    start, end = float(window[0]), float(window[1])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(422, "KanaWhisperのwindowが不正です") from exc
                if (
                    not math.isfinite(start + end)
                    or start < 0
                    or end <= start
                    or end - start > KANA_CONTEXT_MAX_SEC
                    or start < previous_start
                ):
                    raise HTTPException(422, "KanaWhisperのwindowが不正です")
                previous_start = start
        elif kind == "demucs":
            from .separation import DEMUCS_MODEL

            if str(parsed.get("model") or DEMUCS_MODEL) != DEMUCS_MODEL:
                raise HTTPException(422, "許可されていないDemucsモデルです")

        job_id = uuid.uuid4().hex
        work_dir = state_dir / "jobs" / job_id
        work_dir.mkdir(parents=True, exist_ok=False)
        audio_path = work_dir / "input.audio"
        size = 0
        try:
            with audio_path.open("xb") as output:
                while chunk := await audio.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise HTTPException(413, "音源ファイルが上限を超えています")
                    output.write(chunk)
            if size == 0:
                raise HTTPException(422, "音源ファイルが空です")
            job = scheduler.add(kind, priority, parsed, audio_path, work_dir)
            return {"id": job.id, "status": job.status}
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        finally:
            await audio.close()

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = scheduler.get(job_id)
        if job is None:
            raise HTTPException(404, "推論ジョブがありません")
        return scheduler.snapshot(job)

    @app.get("/v1/jobs/{job_id}/artifacts/{artifact_name}")
    def get_artifact(job_id: str, artifact_name: str) -> FileResponse:
        job = scheduler.get(job_id)
        if (
            job is None
            or job.kind != "demucs"
            or job.status != "done"
            or artifact_name not in DEMUCS_ARTIFACTS
        ):
            raise HTTPException(404, "推論成果物がありません")
        artifact = job.work_dir / "output" / artifact_name
        if not artifact.is_file():
            raise HTTPException(404, "推論成果物がありません")
        return FileResponse(artifact, media_type="audio/wav", filename=artifact_name)

    @app.delete("/v1/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str) -> None:
        if not scheduler.cancel_and_remove(job_id):
            raise HTTPException(404, "推論ジョブがありません")

    app.state.scheduler = scheduler
    return app
