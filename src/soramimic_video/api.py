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
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import runproc, synth_estimate
from .layout import LAYOUTS_DIR, builtin_layout_names, load_layout, parse_layout

logger = logging.getLogger(__name__)

API_KEY_ENV = "SORAMIMIC_VIDEO_API_KEY"
STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[2]
# soramimic editor(submodule)のビルド出力。scripts/build-editor.sh で生成する。
# /editor/ にマウントして同一オリジン配信し、WebUIからiframeで埋め込む(A-2)。
DEFAULT_EDITOR_DIST = REPO_ROOT / "external" / "soramimic" / "frontend" / "dist"
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
    synthesizer = job.params.get("synthesizer", "neutrino")
    is_voicevox = synthesizer == "voicevox"
    # VOICEVOXは速く進捗内訳も出ないので、NEUTRINO用の所要見積り・実績記録は行わない
    store: Path | None = None if is_voicevox else config.get("throughput_store")
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
            synthesizer=synthesizer,
            voicevox_url=config.get("voicevox_url", "http://127.0.0.1:50021"),
            voicevox_style=job.params.get("voicevox_style", 3003),
            voicevox_auto_octave=job.params.get("voicevox_auto_octave", True),
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
            # job.video が絶対パス・job.dir が相対パスの組み合わせでも落ちない
            # よう、両方を resolve してから相対化する。ジョブディレクトリ外の
            # パスはそのまま保存する(_load_existing の
            # status_path.parent / video は絶対パスもそのまま扱える)。
            try:
                data["video"] = str(
                    job.video.resolve().relative_to(job.dir.resolve())
                )
            except ValueError:
                data["video"] = str(job.video)
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
            # ジョブ1件の例外でワーカースレッドごと死なないよう防御する
            # (死ぬと以降のジョブが永久にqueuedのままになる)。
            try:
                self._run_one(job)
            except Exception as exc:  # noqa: BLE001 - ワーカー存続を最優先
                job.status = "error"
                job.error = job.error or f"ワーカー内部エラー: {exc}"
                logger.exception("[job %s] ワーカー内部エラー", job.id)
                try:
                    self._save(job)
                except Exception:
                    logger.exception("[job %s] 状態の保存に失敗", job.id)

    def _run_one(self, job: Job) -> None:
        if job.cancel_event.is_set():
            job.status = "canceled"
            self._save(job)
            return
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
    editor_dist: Path | None = None,
    voicevox_url: str = "http://127.0.0.1:50021",
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
        "voicevox_url": voicevox_url,
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

    # editorの静的ビルド(scripts/build-editor.sh の出力)があれば /editor/ で配信する。
    # 無くてもサーバーは起動する(WebUIはeditor連携ボタンを隠すだけ)。
    editor_root = (editor_dist or DEFAULT_EDITOR_DIST).resolve()
    editor_available = (editor_root / "editor.html").is_file()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # 同梱サンプル(ふるさと: 詞・曲ともパブリックドメイン、examples/gen_furusato.py で生成)
    @app.get("/api/sample/midi")
    def sample_midi() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sample" / "furusato.mid",
            media_type="audio/midi",
            filename="furusato.mid",
        )

    @app.get("/api/sample/lyrics")
    def sample_lyrics() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sample" / "furusato_lyrics.txt", media_type="text/plain"
        )

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
            "voicevox": _voicevox_config(),
            "host": platform.node(),
            "layouts": builtin_layout_names(),
            "editor": editor_available,
        }

    def _voicevox_config() -> dict[str, Any] | None:
        """VOICEVOXエンジンが起動していればスタイル一覧、いなければNone。

        起動確認はリクエスト時に短いタイムアウトで行う(サーバー起動を
        ブロックしない。エンジンは後から立ち上げてもよい)。
        """
        from .voicevox import list_singers

        try:
            return {"styles": list_singers(str(config["voicevox_url"]), timeout=1.0)}
        except RuntimeError:
            return None

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

    def _wordlist_image_urls(wordlist: str) -> set[str]:
        """単語リストのimage列に実在する画像URLの集合(URL指定プロキシの許可リスト)。"""
        from .convert import resolve_wordlist

        try:
            with open(resolve_wordlist(wordlist), encoding="utf-8") as f:
                return {r["image"] for r in csv.DictReader(f) if r.get("image")}
        except (FileNotFoundError, OSError):
            return set()

    @app.get("/api/wordlist-image", dependencies=[Depends(_require_api_key)])
    def wordlist_image(wordlist: str = "", url: str = "") -> FileResponse:
        """レイアウト編集プレビュー用の画像(WYSIWYG表示向け)。

        url指定時はプレビューのキュー画像を返す。オープンプロキシ化を避けるため、
        指定した単語リストのimage列に実在するURLだけを取得して返す。
        url未指定時は代表行(単語リストの最初の画像あり行)の画像。
        """
        from .video import download_image

        if url:
            if not wordlist.strip() or url not in _wordlist_image_urls(wordlist.strip()):
                raise HTTPException(status_code=404, detail="画像が見つかりません")
            target = url
        else:
            row = _sample_row(wordlist.strip()) if wordlist.strip() else None
            if not row or not row.get("image"):
                raise HTTPException(status_code=404, detail="画像のある行がありません")
            target = row["image"]
        path = download_image(target, jobs_dir.resolve() / "image-cache")
        if path is None:
            raise HTTPException(status_code=404, detail="画像を取得できません")
        return FileResponse(path)

    @app.post("/api/editor-preview", dependencies=[Depends(_require_api_key)])
    async def editor_preview(
        editor: UploadFile,
        wordlist: str = Form(""),
        cue: int = Form(0),
        layout_json: str = Form(""),
        lyrics: str = Form(""),
    ) -> dict[str, Any]:
        """editor書き出しJSONの変換結果に基づく、キュー1枚ぶんのプレビューデータ。

        レイアウト編集画面のプレビューを、単語リストの代表行1件ではなく実際の
        変換結果(replaced単語列)で描くための元データ。cueで動画のキュー順に送る。
        """
        from .editor_io import build_editor_preview

        raw = await editor.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="editorのJSONが読めません") from exc
        # 編集中のレイアウトがあれば、そのフィルタ・要素でキューを組む(なければ既定)
        layout_obj = load_layout(None)
        if layout_json.strip():
            try:
                layout_obj = parse_layout(json.loads(layout_json), "layout_json")
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"レイアウトJSONが読めません: {exc}"
                ) from exc
        try:
            result = build_editor_preview(
                payload, wordlist.strip() or None, layout_obj, lyrics
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cues = result["cues"]
        total = len(cues)
        if total == 0:
            return {"total": 0, "index": 0, "wordlist": result["wordlist"]}
        index = max(0, min(cue, total - 1))
        item = cues[index]
        image_url = ""
        if item["image"]:
            image_url = "/api/wordlist-image?" + urlencode(
                {"wordlist": result["wordlist"], "url": item["image"]}
            )
        return {
            "total": total,
            "index": index,
            "wordlist": result["wordlist"],
            "data": item["data"],
            "use_fallback": item["use_fallback"],
            "parody_text": item["parody_text"],
            "original_text": item["original_text"],
            "image_url": image_url,
        }

    @app.post("/api/jobs", dependencies=[Depends(_require_api_key)])
    async def create_job(
        midi: UploadFile,
        editor: UploadFile | None = None,
        lyrics: str = Form(""),
        model: str = Form("MERROW"),
        synthesizer: str = Form("neutrino"),
        voicevox_style: int = Form(3003),
        voicevox_auto_octave: bool = Form(True),
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
        if synthesizer not in ("neutrino", "voicevox"):
            raise HTTPException(
                status_code=422, detail="synthesizerは neutrino か voicevox です"
            )
        params = {
            "model": model.strip() or "MERROW",
            "synthesizer": synthesizer,
            "voicevox_style": voicevox_style,
            "voicevox_auto_octave": voicevox_auto_octave,
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

    # ---- 同梱editor(/editor/)向けの配信・シード(A-2) ----
    # 以下のルートは StaticFiles マウントより前に登録して優先させる
    # (単語リストは submodule内のダミーではなく external/soramimic-wordlists の
    #  正データを、kuromoji辞書は Content-Encoding を付けず素のバイナリで返す)。

    @app.get("/editor/wordlists/{name}.csv")
    def editor_wordlist(name: str) -> FileResponse:
        """editorのDB構築(buildDatabase)が取りに来る単語リストCSVを返す。

        editor JSONの wordlist.filepath = "wordlists/<stem>.csv" が
        /editor/wordlists/<stem>.csv に解決される。実体は
        external/soramimic-wordlists の該当CSV。
        """
        from .convert import resolve_wordlist

        if not re.fullmatch(r"[\w-]+", name):
            raise HTTPException(status_code=404, detail="単語リストが見つかりません")
        try:
            path = resolve_wordlist(name)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="単語リストが見つかりません"
            ) from exc
        return FileResponse(path, media_type="text/csv")

    @app.get("/editor/kuromoji/dict/{name}")
    def editor_kuromoji_dict(name: str) -> FileResponse:
        """kuromojiの辞書(.dat.gz)を素のバイナリで返す。

        kuromoji自身が gzip 解凍するので、Content-Encoding: gzip を付けると
        ブラウザが二重解凍して壊れる。octet-stream + no-transform で配る
        (vite の serveDictAsBinary プラグインと同じ扱い)。
        """
        if not editor_available or not re.fullmatch(r"[\w.-]+", name):
            raise HTTPException(status_code=404, detail="辞書が見つかりません")
        path = editor_root / "kuromoji" / "dict" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="辞書が見つかりません")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-transform"},
        )

    @app.post("/api/editor-session", dependencies=[Depends(_require_api_key)])
    async def editor_session(
        midi: UploadFile,
        lyrics: str = Form(""),
        wordlist: str = Form(""),
        where: str = Form(""),
    ) -> dict[str, Any]:
        """MIDI+単語リストから変換済みeditorセッションJSONを組んで返す。

        WebUIがこれを sessionStorage["soramimic-editor"] に書いてから
        /editor/editor.html を iframe で開くと、そのまま編集を始められる。
        run_pipeline の analyze→convert 段を同期・ジョブ無しで実行する。
        """
        import tempfile

        from .align import align_lines
        from .convert import convert_project, resolve_wordlist
        from .editor_io import export_editor, save_raw
        from .xfparse import analyze_midi

        midi_bytes = await midi.read()
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        if not wordlist.strip():
            raise HTTPException(
                status_code=422, detail="単語リスト名(wordlist)が必要です"
            )
        try:
            resolve_wordlist(wordlist.strip())
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "input.mid").write_bytes(midi_bytes)
            try:
                project = analyze_midi(d / "input.mid")
            except Exception as exc:  # noqa: BLE001 - 壊れたMIDIは400で返す
                raise HTTPException(
                    status_code=400, detail=f"MIDIの解析に失敗しました: {exc}"
                ) from exc
            if lyrics.strip():
                align_lines(project, lyrics.splitlines())
            try:
                raw = convert_project(
                    project,
                    wordlist=wordlist.strip(),
                    where=where.strip() or None,
                    params={},
                )
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            save_raw(raw, d)
            project.save(d)
            path = export_editor(project, d)
            return json.loads(path.read_text(encoding="utf-8"))

    if editor_available:
        # 上のルートで拾わなかった /editor/* は静的ビルドから配信する。
        # html=True で /editor/ と /editor/editor.html が引ける。
        app.mount(
            "/editor",
            StaticFiles(directory=editor_root, html=True),
            name="editor",
        )

    return app
