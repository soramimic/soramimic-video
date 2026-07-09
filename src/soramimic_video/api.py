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

import csv
import json
import logging
import os
import platform
import queue
import re
import secrets
import shutil
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

from . import runproc, synth_estimate
from .layout import LAYOUTS_DIR, builtin_layout_names, load_layout, parse_layout

logger = logging.getLogger(__name__)

API_KEY_ENV = "SORAMIMIC_VIDEO_API_KEY"
STATIC_DIR = Path(__file__).parent / "static"
STATUS_FILENAME = "status.json"
THROUGHPUT_FILENAME = "synthesize-throughput.json"
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
    status: str = "queued"  # queued / running / done / canceled / error
    stage: str | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stage_started_at: float | None = None
    stage_progress: int | None = None  # synthesizeの実進捗(%)。NEUTRINO出力から
    stage_estimated_total: float | None = None  # synthesizeの所要秒の見積り
    log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    video: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def _synth_progress(self, elapsed: float) -> tuple[int | None, float | None]:
        """synthesizeステージの進捗率(%)と残り秒の目安を返す。

        NEUTRINOが出す実進捗を優先し、まだ出ていなければ過去実績からの
        見積り(経過秒÷見積り総秒)で補う。どちらも無ければ (None, None)。
        """
        if self.stage_progress:  # 実進捗(1%以上)が取れている
            pct = self.stage_progress
            eta = elapsed * (100 - pct) / pct if 0 < pct < 100 else 0.0
            return pct, eta
        if self.stage_estimated_total and self.stage_estimated_total > 0:
            pct = min(99, int(elapsed / self.stage_estimated_total * 100))
            return pct, max(0.0, self.stage_estimated_total - elapsed)
        return None, None

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
            elapsed = round(time.time() - self.stage_started_at, 1)
            d["stage_elapsed"] = elapsed
            if self.stage == "synthesize":
                pct, eta = self._synth_progress(elapsed)
                if pct is not None:
                    d["stage_progress"] = pct
                    if eta is not None:
                        d["stage_eta_seconds"] = round(eta)
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


def _first_lyric_start(project: Any) -> float:
    """歌詞のある最初の音符の開始秒。音符が無ければ0。"""
    starts = [n.start_sec for n in project.notes if getattr(n, "kana", None)]
    if not starts:
        starts = [n.start_sec for n in project.notes]
    return min(starts) if starts else 0.0


def _trim_wav_head(wav: Path, start: float) -> Path:
    """WAV先頭のstart秒(前奏ぶんの無音)を切り落とす。失敗したら元のWAVを返す。"""
    if start <= 0:
        return wav
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return wav
    out = wav.with_name(wav.stem + "_trimmed.wav")
    proc = runproc.run(
        [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(wav), str(out)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0 or not out.exists():
        logger.warning("プレビューWAVのトリムに失敗しました: %s", proc.stderr[-500:])
        return wav
    return out


def _truncate_project(project: Any, seconds: float, start: float = 0.0) -> None:
    """プレビュー用に start 秒から seconds 秒ぶんの音符・行だけ残す。"""
    end = start + seconds
    kept = [n for n in project.notes if start <= n.start_sec < end]
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

    preview_sec = float(job.params.get("preview") or 0)
    if preview_sec > 0:
        # プレビュー: 空耳変換(convert/import-editor)は行わず、歌い出しから
        # preview 秒ぶんを元歌詞(XFカナ)のまま合成して返す。モデル・移調の
        # 当たり確認が目的なので、ミックス・動画は作らない
        lyric_start = _first_lyric_start(project)
        _truncate_project(project, preview_sec, start=lyric_start)
        wav = _run_synthesize(job, config, project, synthesize)
        assert wav is not None
        # 合成WAVは楽譜の絶対時刻を保つため前奏ぶんの無音が頭に付く。
        # 歌い出しの少し手前まで切り落として即再生できるようにする
        return _trim_wav_head(wav, max(0.0, lyric_start - 0.5))

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

    _run_synthesize(job, config, project, synthesize)
    with _stage(job, "mix"):
        mix(project, d, soundfont=config.get("soundfont"))
    with _stage(job, "video"):
        # レイアウトの優先順: ジョブのJSON > ジョブの名前指定 > サーバー既定(--layout)
        layout: str | None = str(d / "layout.json")
        if not (d / "layout.json").exists():
            layout = job.params.get("layout") or config.get("layout")
        return make_video(
            project,
            d,
            font=config.get("font") or default_font(),
            image_cache=config.get("image_cache"),
            layout=layout,
        )


@contextmanager
def _stage(job: Job, name: str):
    if job.cancel_event.is_set():
        raise runproc.Cancelled()
    job.stage = name
    job.stage_started_at = time.time()
    job.stage_progress = None
    job.stage_estimated_total = None
    logger.info("[job %s] ステージ開始: %s", job.id, name)
    yield
    seconds = round(time.time() - job.stage_started_at, 1)
    job.stages.append({"name": name, "seconds": seconds})
    logger.info("[job %s] ステージ完了: %s (%.1f秒)", job.id, name, seconds)


def _run_synthesize(job: Job, config: dict[str, Any], project: Any, synthesize) -> Any:
    """synthesizeステージを実行し、進捗率と残り時間の目安を job に反映する。

    NEUTRINOの進捗出力を job.stage_progress に、過去実績からの所要見積りを
    job.stage_estimated_total に入れる(to_dict がこれらから %/残り秒を出す)。
    成功後は今回の実績を throughput ストアに記録して次回の見積りに使う。
    """
    store: Path | None = config.get("throughput_store")
    score_seconds = max((n.end_sec for n in project.notes), default=0.0)
    with _stage(job, "synthesize"):
        if store is not None:
            job.stage_estimated_total = synth_estimate.estimate_seconds(
                store, score_seconds
            )

        def on_progress(frac: float) -> None:
            job.stage_progress = max(0, min(100, round(frac * 100)))

        result = synthesize(
            project,
            job.dir,
            model=job.params["model"],
            threads=config.get("threads", 4),
            transpose=job.params.get("transpose", 0),
            progress_cb=on_progress,
        )
        if store is not None and job.stage_started_at is not None:
            synth_estimate.record_run(
                store, score_seconds, time.time() - job.stage_started_at
            )
    return result


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
        layout_json: str = "",
    ) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "input.mid").write_bytes(midi)
        if editor:
            (job_dir / "editor.json").write_bytes(editor)
        if lyrics.strip():
            (job_dir / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        if layout_json.strip():
            (job_dir / "layout.json").write_text(layout_json, encoding="utf-8")
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

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status not in ("queued", "running"):
            return job
        job.cancel_event.set()
        if job.status == "running":
            # 実行中のNEUTRINO/ffmpeg等をプロセスグループごと止める。
            # ワーカーは1本なので、実行中プロセス=このジョブのもの
            runproc.kill_current()
        else:
            job.status = "canceled"
            self._save(job)
        return job

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job.cancel_event.is_set():
                job.status = "canceled"
                self._save(job)
                continue
            handler = _JobLogHandler(job)
            logging.getLogger("soramimic_video").addHandler(handler)
            job.status = "running"
            job.started_at = time.time()
            runproc.set_cancel_check(job.cancel_event.is_set)
            self._save(job)
            try:
                job.video = run_pipeline(job, self.config)
                if job.cancel_event.is_set():
                    raise runproc.Cancelled()
                job.status = "done"
            except runproc.Cancelled:
                job.status = "canceled"
                logger.info("[job %s] 中断されました", job.id)
            except Exception as exc:  # noqa: BLE001 - ジョブ失敗はAPI応答に載せる
                if job.cancel_event.is_set():
                    # 中断でプロセスをkillした結果のエラーは「中断」として扱う
                    job.status = "canceled"
                    logger.info("[job %s] 中断されました", job.id)
                else:
                    job.status = "error"
                    job.error = str(exc)
                    job.log.append(traceback.format_exc())
                    logger.exception("[job %s] 失敗", job.id)
            finally:
                runproc.set_cancel_check(None)
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
    layout: str | None = None,
) -> FastAPI:
    logging.getLogger("soramimic_video").setLevel(logging.INFO)
    config = {
        # 単語画像はジョブをまたいで共有する(初回ジョブの動画ステージが
        # 画像ダウンロードで数分かかるため。2回目以降はほぼゼロになる)
        "image_cache": jobs_dir.resolve() / "image-cache",
        "soundfont": resolve_soundfont(soundfont),
        "font": font or default_font(),
        "threads": threads,
        "layout": layout,
        # 合成の所要時間の目安(曲秒あたりの実処理秒)を実行ごとに記録して次回に使う
        "throughput_store": jobs_dir.resolve() / THROUGHPUT_FILENAME,
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
            "layouts": builtin_layout_names(),
        }

    @app.get("/api/layouts/{name}", dependencies=[Depends(_require_api_key)])
    def get_layout(name: str) -> dict[str, Any]:
        """組み込みレイアウトのJSONを返す(UIの「編集用に読み込む」向け)。"""
        if not re.fullmatch(r"[\w-]+", name):
            raise HTTPException(status_code=404, detail="レイアウトが見つかりません")
        path = LAYOUTS_DIR / f"{name}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="レイアウトが見つかりません")
        return json.loads(path.read_text(encoding="utf-8"))

    def _sample_row(wordlist: str) -> dict[str, str] | None:
        """レイアウト編集のプレビューに使う代表行(画像のある最初の行、なければ先頭)。"""
        from .convert import resolve_wordlist

        try:
            with open(resolve_wordlist(wordlist), encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except (FileNotFoundError, OSError):
            return None
        return next((r for r in rows if r.get("image")), rows[0] if rows else None)

    @app.get("/api/wordlist-columns", dependencies=[Depends(_require_api_key)])
    def wordlist_columns(wordlist: str = "") -> dict[str, Any]:
        """単語リストの列名一覧と代表行(レイアウト編集のWYSIWYG表示向け)。

        リストが未指定・見つからない場合も、替え歌単語のフィールドは返す。
        """
        from .convert import resolve_wordlist

        cols: list[str] = []
        row = None
        if wordlist.strip():
            try:
                with open(resolve_wordlist(wordlist.strip()), encoding="utf-8") as f:
                    cols = next(csv.reader(f), [])
            except (FileNotFoundError, OSError):
                pass
            row = _sample_row(wordlist.strip())
        word_fields = ["surface", "original", "kana", "original_surface", "originalkana"]
        if row:
            # kana等はCSVの列ではなく変換後の替え歌単語のフィールド。
            # プレビューでも空にならないよう代表行から補う
            row = {
                "kana": row.get("pronunciation") or row.get("surface", ""),
                "original_surface": "(元歌詞の対応部分)",
                "originalkana": "(モトカシ)",
                **row,
            }
        return {
            "columns": list(dict.fromkeys([*word_fields, *cols])),
            "row": row,
        }

    @app.get("/api/wordlist-image", dependencies=[Depends(_require_api_key)])
    def wordlist_image(wordlist: str = "") -> FileResponse:
        """代表行の画像(レイアウト編集のWYSIWYG表示向け)。"""
        from .video import download_image

        row = _sample_row(wordlist.strip()) if wordlist.strip() else None
        if not row or not row.get("image"):
            raise HTTPException(status_code=404, detail="画像のある行がありません")
        path = download_image(row["image"], jobs_dir.resolve() / "image-cache")
        if path is None:
            raise HTTPException(status_code=404, detail="画像を取得できません")
        return FileResponse(path)

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
        layout: str = Form(""),
        layout_json: str = Form(""),
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
        # プレビューは元歌詞をそのまま歌わせるので替え歌の入力は不要
        if preview <= 0 and editor_bytes is None and not wordlist.strip():
            raise HTTPException(
                status_code=422,
                detail="editorの書き出しJSONか単語リスト名(wordlist)のどちらかが必要です",
            )
        layout = layout.strip()
        layout_json = layout_json.strip()
        # 投入前に検証してエラーはフォームに返す(ジョブを走らせてから落とさない)
        if layout_json:
            try:
                parse_layout(json.loads(layout_json), "layout_json")
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"レイアウトJSONが読めません: {exc}"
                ) from exc
        elif layout:
            try:
                load_layout(layout)
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = {
            "model": model.strip() or "MERROW",
            "transpose": transpose,
            "preview": max(0.0, min(preview, 60.0)),
            "wordlist": wordlist.strip(),
            "where": where.strip(),
            "layout": layout,
            "parody_source": "editor" if editor_bytes else "convert",
            "midi_filename": midi.filename,
        }
        job = manager.create(midi_bytes, editor_bytes, lyrics, params, layout_json=layout_json)
        return {"id": job.id}

    @app.get("/api/jobs", dependencies=[Depends(_require_api_key)])
    def list_jobs() -> list[dict[str, Any]]:
        jobs = sorted(manager.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict(with_log=False) for j in jobs[:30]]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
    def get_job(job_id: str) -> dict[str, Any]:
        return manager.get(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(_require_api_key)])
    def cancel_job(job_id: str) -> dict[str, Any]:
        return manager.cancel(job_id).to_dict(with_log=False)

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
