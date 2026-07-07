"""動画生成APIサーバー(ローカル/自宅サーバー向け)。

POST /api/jobs にXF MIDI(+soramimic editorの書き出しJSON、元歌詞)を投げると
analyze → import-editor(またはconvert) → synthesize → mix → video を
バックグラウンドで順に実行する。進捗は GET /api/jobs/{id}、完成動画は
GET /api/jobs/{id}/video で取得する。GET / に簡易Web UIを同梱。

環境変数 SORAMIMIC_VIDEO_API_KEY を設定すると全APIで X-API-Key ヘッダ
(または api_key クエリ)を必須にする(LAN外に公開するとき用)。
依存は `pip install -e '.[api]'` で入る。NEUTRINOの実行が重いので
ワーカーは1本、ジョブは投入順に直列実行する。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import secrets
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger(__name__)

API_KEY_ENV = "SORAMIMIC_VIDEO_API_KEY"
STATIC_DIR = Path(__file__).parent / "static"
STATUS_FILENAME = "status.json"
DEFAULT_SOUNDFONTS = ("/usr/share/sounds/sf2/FluidR3_GM.sf2",)


def default_font() -> str:
    return "Hiragino Sans" if platform.system() == "Darwin" else "Noto Sans CJK JP"


def resolve_soundfont(soundfont: str | None) -> str | None:
    """引数 > 環境変数SOUNDFONT > OS標準の場所、の順で伴奏用sf2を決める。"""
    if soundfont:
        return soundfont
    if os.environ.get("SOUNDFONT"):
        return os.environ["SOUNDFONT"]
    for cand in DEFAULT_SOUNDFONTS:
        if Path(cand).exists():
            return cand
    return None


def list_models() -> list[str]:
    root = os.environ.get("NEUTRINO_ROOT")
    if not root:
        return []
    model_dir = Path(root).expanduser() / "model"
    if not model_dir.is_dir():
        return []
    return sorted(p.name for p in model_dir.iterdir() if p.is_dir())


@dataclass
class Job:
    id: str
    dir: Path
    params: dict[str, Any]
    status: str = "queued"  # queued / running / done / error
    stage: str | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stage_started_at: float | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    video: Path | None = None

    def to_dict(self, with_log: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "stages": self.stages,
            "params": self.params,
            "error": self.error,
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(
                timespec="seconds"
            ),
        }
        if self.status == "running" and self.stage_started_at:
            d["stage_elapsed"] = round(time.time() - self.stage_started_at, 1)
        if self.started_at and self.finished_at:
            d["total_seconds"] = round(self.finished_at - self.started_at, 1)
        if self.status == "done" and self.video:
            d["video_url"] = f"/api/jobs/{self.id}/video"
            d["result_kind"] = "audio" if self.video.suffix == ".wav" else "video"
        if with_log:
            d["log"] = list(self.log)
        return d


class _JobLogHandler(logging.Handler):
    """パイプラインのログをジョブごとに取り込む(ワーカーは1本なので混線しない)。"""

    def __init__(self, job: Job) -> None:
        super().__init__(level=logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.job.log.append(self.format(record))


def _truncate_project(project: Any, seconds: float) -> None:
    """プレビュー用に冒頭seconds秒ぶんの音符・行だけ残す。"""
    kept = [n for n in project.notes if n.start_sec < seconds]
    kept_ids = {n.id for n in kept}
    project.notes = kept
    lines = []
    for line in project.lines:
        line.note_ids = [nid for nid in line.note_ids if nid in kept_ids]
        if line.note_ids:
            lines.append(line)
    project.lines = lines


def run_pipeline(job: Job, config: dict[str, Any]) -> Path:
    """analyze〜videoを順に実行して完成動画のパスを返す(ワーカースレッドから呼ぶ)。"""
    from .align import align_lines
    from .editor_io import import_editor, save_raw
    from .mix import mix
    from .synthesize import synthesize
    from .video import make_video
    from .xfparse import analyze_midi

    d = job.dir
    with _stage(job, "analyze"):
        project = analyze_midi(d / "input.mid")
        lyrics_path = d / "lyrics.txt"
        if lyrics_path.exists():
            align_lines(project, lyrics_path.read_text(encoding="utf-8").splitlines())
        project.save(d)

    if (d / "editor.json").exists():
        with _stage(job, "import-editor"):
            import_editor(project, d, d / "editor.json")
            project.save(d)
    else:
        from .convert import convert_project

        with _stage(job, "convert"):
            raw = convert_project(
                project,
                wordlist=job.params["wordlist"],
                where=job.params.get("where") or None,
                params={},
            )
            save_raw(raw, d)
            project.save(d)

    preview_sec = float(job.params.get("preview") or 0)
    if preview_sec > 0:
        # プレビュー: 冒頭だけ歌声を合成して返す(ミックス・動画は作らない)
        _truncate_project(project, preview_sec)
        with _stage(job, "synthesize"):
            wav = synthesize(
                project,
                d,
                model=job.params["model"],
                threads=config.get("threads", 4),
                transpose=job.params.get("transpose", 0),
            )
        assert wav is not None
        return wav

    with _stage(job, "synthesize"):
        synthesize(
            project,
            d,
            model=job.params["model"],
            threads=config.get("threads", 4),
            transpose=job.params.get("transpose", 0),
        )
    with _stage(job, "mix"):
        mix(project, d, soundfont=config.get("soundfont"))
    with _stage(job, "video"):
        return make_video(
            project,
            d,
            font=config.get("font") or default_font(),
            image_cache=config.get("image_cache"),
        )


@contextmanager
def _stage(job: Job, name: str):
    job.stage = name
    job.stage_started_at = time.time()
    logger.info("[job %s] ステージ開始: %s", job.id, name)
    yield
    seconds = round(time.time() - job.stage_started_at, 1)
    job.stages.append({"name": name, "seconds": seconds})
    logger.info("[job %s] ステージ完了: %s (%.1f秒)", job.id, name, seconds)


class JobManager:
    """ジョブの受付・直列実行・状態保持。状態は各ジョブディレクトリにも永続化する。"""

    def __init__(self, jobs_dir: Path, config: dict[str, Any]) -> None:
        self.jobs_dir = jobs_dir
        self.config = config
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[Job] = queue.Queue()
        self._load_existing()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _load_existing(self) -> None:
        if not self.jobs_dir.is_dir():
            return
        for status_path in sorted(self.jobs_dir.glob(f"*/{STATUS_FILENAME}")):
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job = Job(
                id=data["id"],
                dir=status_path.parent,
                params=data.get("params", {}),
                status=data.get("status", "error"),
                stages=data.get("stages", []),
                error=data.get("error"),
            )
            if data.get("created_at"):
                job.created_at = datetime.fromisoformat(data["created_at"]).timestamp()
            if job.status in ("queued", "running"):
                job.status = "error"
                job.error = "サーバー再起動により中断されました"
            video = status_path.parent / data.get("video", "")
            if data.get("video") and video.exists():
                job.video = video
            self.jobs[job.id] = job

    def create(
        self,
        midi: bytes,
        editor: bytes | None,
        lyrics: str,
        params: dict[str, Any],
    ) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "input.mid").write_bytes(midi)
        if editor:
            (job_dir / "editor.json").write_bytes(editor)
        if lyrics.strip():
            (job_dir / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        job = Job(id=job_id, dir=job_dir, params=params)
        with self._lock:
            self.jobs[job_id] = job
        self._save(job)
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="ジョブが見つかりません")
        return job

    def _save(self, job: Job) -> None:
        data = job.to_dict(with_log=False)
        if job.video:
            data["video"] = job.video.name if job.video.parent == job.dir else str(
                job.video.relative_to(job.dir)
            )
        (job.dir / STATUS_FILENAME).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            handler = _JobLogHandler(job)
            logging.getLogger("soramimic_video").addHandler(handler)
            job.status = "running"
            job.started_at = time.time()
            self._save(job)
            try:
                job.video = run_pipeline(job, self.config)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - ジョブ失敗はAPI応答に載せる
                job.status = "error"
                job.error = str(exc)
                job.log.append(traceback.format_exc())
                logger.exception("[job %s] 失敗", job.id)
            finally:
                job.stage = None
                job.finished_at = time.time()
                logging.getLogger("soramimic_video").removeHandler(handler)
                self._save(job)


def _require_api_key(request: Request) -> None:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        return
    supplied = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if not supplied or not secrets.compare_digest(supplied, key):
        raise HTTPException(status_code=401, detail="APIキーが必要です(X-API-Key)")


def create_app(
    jobs_dir: Path,
    soundfont: str | None = None,
    font: str | None = None,
    threads: int = 4,
) -> FastAPI:
    logging.getLogger("soramimic_video").setLevel(logging.INFO)
    config = {
        # 単語画像はジョブをまたいで共有する(初回ジョブの動画ステージが
        # 画像ダウンロードで数分かかるため。2回目以降はほぼゼロになる)
        "image_cache": jobs_dir.resolve() / "image-cache",
        "soundfont": resolve_soundfont(soundfont),
        "font": font or default_font(),
        "threads": threads,
    }
    manager = JobManager(jobs_dir, config)
    app = FastAPI(title="soramimic-video API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    def get_config(request: Request) -> dict[str, Any]:
        auth_required = bool(os.environ.get(API_KEY_ENV))
        try:
            _require_api_key(request)
        except HTTPException:
            return {"auth_required": True}
        return {
            "auth_required": auth_required,
            "models": list_models(),
            "neutrino": bool(os.environ.get("NEUTRINO_ROOT")),
            "host": platform.node(),
        }

    @app.post("/api/jobs", dependencies=[Depends(_require_api_key)])
    async def create_job(
        midi: UploadFile,
        editor: UploadFile | None = None,
        lyrics: str = Form(""),
        model: str = Form("MERROW"),
        transpose: int = Form(0),
        preview: float = Form(0),
        wordlist: str = Form(""),
        where: str = Form(""),
    ) -> dict[str, Any]:
        midi_bytes = await midi.read()
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        editor_bytes = None
        if editor is not None and editor.filename:
            editor_bytes = await editor.read()
            try:
                json.loads(editor_bytes)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail="editorのJSONが読めません"
                ) from exc
        if editor_bytes is None and not wordlist.strip():
            raise HTTPException(
                status_code=422,
                detail="editorの書き出しJSONか単語リスト名(wordlist)のどちらかが必要です",
            )
        params = {
            "model": model.strip() or "MERROW",
            "transpose": transpose,
            "preview": max(0.0, min(preview, 60.0)),
            "wordlist": wordlist.strip(),
            "where": where.strip(),
            "parody_source": "editor" if editor_bytes else "convert",
            "midi_filename": midi.filename,
        }
        job = manager.create(midi_bytes, editor_bytes, lyrics, params)
        return {"id": job.id}

    @app.get("/api/jobs", dependencies=[Depends(_require_api_key)])
    def list_jobs() -> list[dict[str, Any]]:
        jobs = sorted(manager.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict(with_log=False) for j in jobs[:30]]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
    def get_job(job_id: str) -> dict[str, Any]:
        return manager.get(job_id).to_dict()

    @app.get("/api/jobs/{job_id}/video", dependencies=[Depends(_require_api_key)])
    def get_video(job_id: str) -> FileResponse:
        job = manager.get(job_id)
        if job.status != "done" or not job.video or not job.video.exists():
            raise HTTPException(status_code=409, detail="動画はまだできていません")
        if job.video.suffix == ".wav":  # プレビュー(歌声のみ)
            return FileResponse(
                job.video, media_type="audio/wav", filename=f"preview_{job.id}.wav"
            )
        return FileResponse(
            job.video, media_type="video/mp4", filename=f"soramimic_{job.id}.mp4"
        )

    return app
