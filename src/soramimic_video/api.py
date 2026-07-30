"""動画生成APIサーバー(ローカル/自宅サーバー向け)。

POST /api/jobs にXF MIDI(+soramimic editorの書き出しJSON、元歌詞)を投げると
analyze → import-editor(またはconvert) → synthesize → mix → video を
バックグラウンドで順に実行する。進捗は GET /api/jobs/{id}、完成動画は
GET /api/jobs/{id}/video で取得する。GET / に簡易Web UIを同梱。

環境変数 SORAMIMIC_VIDEO_API_KEY を設定すると全APIで X-API-Key ヘッダ
(または api_key クエリ)を必須にする(LAN外に公開するとき用)。
依存は `pip install -e '.[api]'` で入る。NEUTRINOの実行が重いので
ワーカーは1本、ジョブは投入順に直列実行する。

SORAMIMIC_PUBLIC=1 を設定すると「公開モード」になり、匿名セッション
(HttpOnly cookie)ごとにジョブを分離し、キュー上限・日次クォータ・
曲長上限で投入を制限する。環境変数を何も設定しなければ従来と同じ挙動
(全ジョブが全員から見え、制限なし)。詳細は docs/public-mode.md を参照。
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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import runproc, synth_estimate
from . import wordlist_csv as wordlist_csv_mod
from . import wordlist_zip as wordlist_zip_mod
from .layout import (
    LAYOUTS_DIR,
    builtin_layout_names,
    load_layout,
    load_wordlist_layouts,
    parse_layout,
)
from .soramimic_engine import start_warmup_thread
from .thumbnail_preview import RateLimiter, preview_cache_dir

logger = logging.getLogger(__name__)

API_KEY_ENV = "SORAMIMIC_VIDEO_API_KEY"
# ---- 公開モード(一般公開インスタンス)向けの環境変数 ----
# いずれも未設定なら従来どおりの挙動(制限なし・ジョブは全員から見える)。
PUBLIC_ENV = "SORAMIMIC_PUBLIC"  # 1/true で公開モード
QUEUE_LIMIT_ENV = "SORAMIMIC_QUEUE_LIMIT"  # 待機+実行中ジョブの上限
DAILY_QUOTA_ENV = "SORAMIMIC_DAILY_QUOTA"  # セッションあたり24時間の投入上限
MAX_SONG_SECONDS_ENV = "SORAMIMIC_MAX_SONG_SECONDS"  # 入力MIDIの演奏時間の上限(秒)
JOB_TTL_HOURS_ENV = "SORAMIMIC_JOB_TTL_HOURS"  # 完了後に自動削除するまでの時間(0=無効)
SAMPLES_DIR_ENV = "SORAMIMIC_SAMPLES_DIR"  # 同梱サンプル曲の差し替え先
TURNSTILE_SECRET_ENV = "TURNSTILE_SECRET_KEY"  # Cloudflare Turnstileの秘密鍵
TURNSTILE_SITE_ENV = "TURNSTILE_SITE_KEY"  # 同・サイトキー(フロントに渡す)
DEFAULT_QUEUE_LIMIT = 5
DEFAULT_DAILY_QUOTA = 5
DEFAULT_MAX_SONG_SECONDS = 420.0
SESSION_COOKIE = "sv_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30日
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
CLEANUP_INTERVAL_SECONDS = 3600  # ジョブ自動削除の巡回間隔
STATIC_DIR = Path(__file__).parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[2]
# soramimic editor(submodule)のビルド出力。scripts/build-editor.sh で生成する。
# /editor/ にマウントして同一オリジン配信し、WebUIからiframeで埋め込む(A-2)。
DEFAULT_EDITOR_DIST = REPO_ROOT / "external" / "soramimic" / "frontend" / "dist"
STATUS_FILENAME = "status.json"
THROUGHPUT_FILENAME = "synthesize-throughput.json"
# アップロードされた自作単語リストを置くジョブ内サブディレクトリ。
# ファイル名(<表示名>.csv)は editor 連携・サムネのリスト名表示に効くので残す。
WORDLIST_DIRNAME = "wordlist"
# zipで来た自作リストの画像を置く場所(<ジョブ>/wordlist/images/img_xxx.png)
WORDLIST_IMAGES_DIRNAME = "images"
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


def is_public_mode() -> bool:
    """公開モード(SORAMIMIC_PUBLIC)かどうか。未設定なら従来どおりの非公開モード。"""
    return os.environ.get(PUBLIC_ENV, "").strip().lower() not in ("", "0", "false", "no")


def _env_float(name: str, default: float) -> float:
    """数値の環境変数を読む。未設定・読めない値は既定値にフォールバックする。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("環境変数 %s の値が数値ではありません: %r", name, raw)
        return default


def samples_dir() -> Path:
    """同梱サンプル曲のディレクトリ。SORAMIMIC_SAMPLES_DIR があればそちらを使う。"""
    override = os.environ.get(SAMPLES_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else STATIC_DIR / "sample"


def load_samples() -> list[dict[str, Any]]:
    """samples.json の中身。読めなければ空リスト(サンプル無しとして扱う)。"""
    try:
        raw = (samples_dir() / "samples.json").read_text(encoding="utf-8")
    except OSError:
        logger.warning("samples.json を読めません: %s", samples_dir())
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("samples.json が壊れています: %s", samples_dir())
        return []
    return [e for e in entries if isinstance(e, dict)]


def sample_entry(sample_id: str) -> dict[str, Any] | None:
    """samples.json の1件(そのIDが無ければ None)。"""
    for entry in load_samples():
        if entry.get("id") == sample_id:
            return entry
    return None


def turnstile_site_key() -> str:
    """Turnstileのサイトキー。秘密鍵とサイトキーが両方揃っているときだけ返す。"""
    site = os.environ.get(TURNSTILE_SITE_ENV, "").strip()
    secret = os.environ.get(TURNSTILE_SECRET_ENV, "").strip()
    return site if site and secret else ""


def verify_turnstile(token: str, remote_ip: str | None = None) -> bool:
    """Cloudflare TurnstileのトークンをCloudflareに問い合わせて検証する。

    TURNSTILE_SECRET_KEY が未設定なら検証自体を行わない(常にTrue)。
    Cloudflareに繋がらない場合は通してしまわず False にする(bot対策優先)。
    """
    secret = os.environ.get(TURNSTILE_SECRET_ENV, "").strip()
    if not secret:
        return True
    if not token:
        return False
    import requests

    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        res = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10)
        return bool(res.json().get("success"))
    except (requests.RequestException, ValueError):
        logger.warning("Turnstileの検証に失敗しました(通信エラー)")
        return False


def fmt_duration_ja(seconds: float) -> str:
    """秒数を「約7分」「約40秒」のように読める日本語にする(制限の説明文用)。"""
    if seconds < 60:
        return f"約{round(seconds)}秒"
    return f"約{round(seconds / 60)}分"


def song_seconds(midi_bytes: bytes) -> float | None:
    """入力MIDIの演奏時間(最後の音符の終わり)の秒数。解析できなければNone。

    曲長上限の判定用。解析は run_pipeline と同じ xfparse.analyze_midi を使う
    (壊れたMIDIはここで弾かず、従来どおりジョブ実行時のエラーに任せる)。
    """
    import tempfile

    from .xfparse import analyze_midi

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "input.mid"
        path.write_bytes(midi_bytes)
        try:
            project = analyze_midi(path)
        except Exception:  # noqa: BLE001 - 解析不能なら上限判定をスキップする
            logger.info("曲長の判定用のMIDI解析に失敗しました(上限チェックはスキップ)")
            return None
    return max((n.end_sec for n in project.notes), default=0.0)


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
    # 公開モードでの持ち主(匿名セッションID)。非公開モードでは常にNone(全員が見る)
    owner: str | None = None
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
    # videoステージが実際に使ったレイアウトの出どころ(resolve_layout が入れる)。
    # "name:<レイアウト名>" / "json:layout.json" など。あとから食い違いを追うため
    layout_source: str | None = None
    video: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def thumbnail(self) -> Path:
        """サムネ画像(video ステージが作る)のパス。未生成なら存在しない。"""
        from .thumbnail import THUMBNAIL_FILENAME

        return self.dir / THUMBNAIL_FILENAME

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
        if self.layout_source:
            d["layout_source"] = self.layout_source
        if self.started_at and self.finished_at:
            d["total_seconds"] = round(self.finished_at - self.started_at, 1)
        if self.status == "done" and self.video:
            d["video_url"] = f"/api/jobs/{self.id}/video"
            d["result_kind"] = "audio" if self.video.suffix == ".wav" else "video"
            if self.thumbnail.exists():
                d["thumbnail_url"] = f"/api/jobs/{self.id}/thumbnail"
        if with_log:
            d["log"] = list(self.log)
        return d


def _clean_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")[:40]


def custom_wordlist_name(filename: str) -> str:
    """アップロードされたCSVのファイル名から、リストの表示名(=保存名)を作る。

    ジョブディレクトリ内のファイル名になるので、区切り文字・ドットは潰す。
    この名前はサムネのキャプション・ダウンロード名にもそのまま出る。
    """
    stem = re.sub(r"\.[^.]*$", "", filename or "")
    return _clean_name(stem).replace(".", "_") or "custom"


async def _read_wordlist_images(files: list[UploadFile]) -> dict[str, bytes]:
    """multipartで来た単語画像を {ファイル名: 中身} にする(貼り付けテキスト用)。

    1枚あたりは上限+1バイトだけ読む(上限超えは wordlist_zip 側がzipのときと同じ
    理由で断る)。合計も読みながら見て、zip1つぶんの上限を超えたらそこで止める。
    """
    per_file = wordlist_zip_mod.max_image_bytes()
    total_limit = wordlist_zip_mod.max_zip_bytes()
    out: dict[str, bytes] = {}
    total = 0
    for f in files:
        if not f.filename:
            continue  # ファイルを選ばなかった入力は空のパートで来る
        data = await f.read(per_file + 1)
        total += len(data)
        if total > total_limit:
            raise wordlist_csv_mod.WordlistCsvError(
                f"入力が大きすぎます(上限は合計{total_limit / 1024 / 1024:.1f}MBです)。"
            )
        out[f.filename] = data
    return out


def custom_wordlist_path(job: Job) -> Path | None:
    """このジョブが自作リストを持っていればそのCSVパス(無ければ None)。"""
    name = str(job.params.get("wordlist_csv") or "")
    if not name:
        return None
    path = job.dir / WORDLIST_DIRNAME / name
    return path if path.exists() else None


def _store_wordlist_images(wl_dir: Path, text: str, images: dict[str, bytes]) -> str:
    """zip同梱の画像をジョブ内に書き出し、CSVの image 列を実体のパスに書き換える。

    動画生成側(video.download_image)は「``://`` を含まない値」をローカルパスとして
    キャッシュに取り込むので、ここで絶対パスにしておけば描画側は素通しで画像が出る。
    保存名は wordlist_zip が付けた ``img_<sha1>.png`` だけを通す(外から来た名前で
    ジョブディレクトリの外に書かないため)。
    """
    # jobs_dir が相対パス(cli既定の work/api-jobs 等)でも、CSVに書くパスは絶対にする
    # (描画はcwd依存で動くが、ログや将来のcwd変更で壊れないように)
    img_dir = (wl_dir / WORDLIST_IMAGES_DIRNAME).resolve()
    img_dir.mkdir(parents=True, exist_ok=True)
    safe = {n for n in images if re.fullmatch(r"img_[0-9a-f]{16}\.(png|jpg)", n)}
    for name in sorted(safe):
        (img_dir / name).write_bytes(images[name])
    # 正規化済みテキストはクオート無しの ",".join なので、csvモジュールではなく
    # エンジンと同じ split(",") で読み直す(値に「"」が残っていても崩れないように)
    lines = text.splitlines()
    if not lines or "image" not in lines[0].split(","):
        return text
    i = lines[0].split(",").index("image")
    out = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        if i < len(cells) and cells[i] in safe:
            cells[i] = str(img_dir / cells[i])
        out.append(",".join(cells))
    return "\n".join(out)


def _job_slug(job: Job) -> tuple[str, str]:
    """ダウンロード名に使う (曲名, 単語リスト名)。"""
    # Path.stem だと曲名中の「/」でパス扱いになるので拡張子だけ正規表現で落とす
    song = _clean_name(re.sub(r"\.[^.]*$", "", job.params.get("midi_filename") or ""))
    return song, _clean_name(job.params.get("wordlist") or "")


def _download_filename(job: Job) -> str:
    """曲名・単語リスト入りのダウンロード名。落とした後もどのジョブか分かるように。"""
    song, wordlist = _job_slug(job)
    if job.video is not None and job.video.suffix == ".wav":  # プレビュー(歌声のみ)
        return "_".join(filter(None, ["preview", song, job.id])) + ".wav"
    return "_".join(filter(None, ["soramimic", song, wordlist, job.id])) + ".mp4"


def song_title_of(params: dict[str, Any]) -> str:
    """サムネに出す曲名。UIが送ってきた曲名(サンプル曲は samples.json の title)を
    優先し、無ければアップロード時のファイル名(midi_filename)を使う。

    ジョブのMIDIは input.mid に固定されるので、曲名は params からしか取れない。
    """
    return str(params.get("song_title") or params.get("midi_filename") or "")


def song_title_kana_of(params: dict[str, Any]) -> str:
    """サムネの曲名変換に使う読み(カタカナ)。分からなければ空文字。

    読みが確定しているのは同梱サンプル曲だけ(samples.json の title_kana)。
    UIはサンプル曲を選ぶと `<サンプルID>.mid` をそのまま送ってくるので、
    midi_filename の拡張子を落としたものでサンプルを引く。
    自分のMIDIを上げた人がたまたま同じファイル名を付けていることもあるので、
    UIが送ってきた曲名がサンプルの曲名と食い違うときは読みを使わない
    (その場合は従来どおり曲名の文字列から変換エンジンが読みを推定する)。
    """
    stem = re.sub(r"\.[^.]*$", "", str(params.get("midi_filename") or "")).strip()
    entry = sample_entry(stem) if stem else None
    if entry is None:
        return ""
    title = str(params.get("song_title") or "").strip()
    if title and title != str(entry.get("title") or ""):
        return ""
    return str(entry.get("title_kana") or "")


def synth_credit_of(params: dict[str, Any], config: dict[str, Any]) -> str:
    """動画に焼き込む歌声合成のクレジット表記(不要なら空文字)。

    VOICEVOXは規約で「VOICEVOX:キャラ名」の表記が必要なので必ず出す
    (キャラ名はエンジンのスタイル一覧から引く。エンジンが落ちていて名前が
    引けないときは「VOICEVOX」だけにする)。NEUTRINOは公式FAQで名称の記載が
    任意なので焼き込まない(ライブラリ個別の規約はWeb UIの「公開時のクレジット
    表記」で案内している)。
    """
    # 既定値のvoicevoxではなくneutrinoで補うのは、synthesizerを記録していない
    # 古いジョブがNEUTRINO時代のものだから(過去ジョブの表記を変えないため据え置く)
    if str(params.get("synthesizer") or "neutrino") != "voicevox":
        return ""
    from .voicevox import list_singers

    style_id = params.get("voicevox_style")
    try:
        styles = list_singers(str(config.get("voicevox_url") or ""), timeout=2.0)
    except (RuntimeError, ValueError) as e:
        logger.warning("VOICEVOXのキャラ名を取得できません(名前なしで表記します): %s", e)
        styles = []
    name = next(
        (s["name"] for s in styles if str(s.get("style_id")) == str(style_id)), ""
    )
    return f"VOICEVOX:{name}" if name else "VOICEVOX"


def _thumbnail_filename(job: Job) -> str:
    """サムネ画像のダウンロード名(動画と同じ命名で拡張子だけpng)。"""
    song, wordlist = _job_slug(job)
    return "_".join(filter(None, ["soramimic", song, wordlist, job.id])) + ".png"


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


PREVIEW_MODES = ("", "head", "high", "low")


def _extreme_line(project: Any, mode: str) -> Any | None:
    """曲の最高音(high)/最低音(low)を含む行を返す。同値なら最初の行。"""
    lines = [ln for ln in project.lines if ln.note_ids]
    if not lines:
        return None
    if mode == "high":
        return max(
            lines, key=lambda ln: max(project.notes[i].midi_note for i in ln.note_ids)
        )
    return min(
        lines, key=lambda ln: min(project.notes[i].midi_note for i in ln.note_ids)
    )


def _preview_window(project: Any, mode: str, seconds: float) -> tuple[float, float]:
    """プレビューで切り出す時間窓 (開始秒, 長さ秒)。

    既定(head)は歌い出しから seconds 秒。high/low は最高音/最低音を含む行
    (フレーズ)1つぶんで、seconds は上限としてだけ効く。
    """
    line = _extreme_line(project, mode) if mode in ("high", "low") else None
    if line is None:
        return _first_lyric_start(project), seconds
    start = project.line_time_range(line)[0]
    # _truncate_project は音符の開始秒で切るので、行末の音符が確実に残り、
    # 次の行の音符は入らないよう「行末の音符の開始 + 微小マージン」で切る
    last_start = project.notes[line.note_ids[-1]].start_sec
    return start, min(seconds, last_start - start + 1e-3)


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
        # プレビュー: 空耳変換(convert/import-editor)は行わず、曲の一部を
        # 元歌詞(XFカナ)のまま合成して返す。モデル・移調の当たり確認が目的
        # なので、ミックス・動画は作らない。どこを切り出すかは preview_mode
        # (歌い出し / 最高音のフレーズ / 最低音のフレーズ)で決まる
        mode = str(job.params.get("preview_mode") or "")
        # 自動オクターブ調整を切り出し後の音域で決めると本番と違うキーで歌って
        # しまう(例: lowモードは曲の最高音が消えて下げ判定が変わる)。
        # 切り出す前の全音符の音高を合成側に渡してキーを本番に揃える
        octave_keys = [n.midi_note for n in project.notes]
        start, seconds = _preview_window(project, mode, preview_sec)
        _truncate_project(project, seconds, start=start)
        wav = _run_synthesize(
            job, config, project, synthesize, octave_keys=octave_keys
        )
        assert wav is not None
        # 合成WAVは楽譜の絶対時刻を保つため前奏ぶんの無音が頭に付く。
        # 切り出した位置の少し手前まで切り落として即再生できるようにする
        return _trim_wav_head(wav, max(0.0, start - 0.5))

    if (d / "editor.json").exists():
        with _stage(job, "import-editor"):
            import_editor(project, d, d / "editor.json")
            project.save(d)
    else:
        from .convert import convert_project, parse_convert_params

        # 自作リストをアップロードしたジョブは、リスト名ではなくジョブ内の
        # CSVを使う。中身はこのジョブ限りなので単語DBの共有キャッシュには載せない
        custom_csv = custom_wordlist_path(job)
        with _stage(job, "convert"):
            raw = convert_project(
                project,
                wordlist=str(custom_csv) if custom_csv else job.params["wordlist"],
                where=job.params.get("where") or None,
                params=parse_convert_params(job.params.get("convert_params")),
                cache_db=custom_csv is None,
            )
            save_raw(raw, d)
            project.save(d)

    _run_synthesize(job, config, project, synthesize)
    # 自動調整が決めたキー変更(song.key_shift)を project.json に残す。
    # 続く mix はこの値だけ伴奏を移調して歌と調を合わせる
    project.save(d)
    with _stage(job, "mix"):
        mix(project, d, soundfont=config.get("soundfont"))
    with _stage(job, "video"):
        layout, job.layout_source = resolve_layout(job, config)
        from .align import parse_granularity_override

        return make_video(
            project,
            d,
            font=config.get("font") or default_font(),
            image_cache=config.get("image_cache"),
            layout=layout,
            granularity=parse_granularity_override(job.params.get("subtitle_granularity")),
            song_title=song_title_of(job.params),
            song_title_kana=song_title_kana_of(job.params),
            synth_credit=synth_credit_of(job.params, config),
        )


LAYOUT_FILENAME = "layout.json"


def resolve_layout(job: Job, config: dict[str, Any]) -> tuple[str | None, str]:
    """videoステージに渡すレイアウト指定と、その出どころ(status.jsonに残す)。

    優先順は ジョブのJSON(layout.json) > ジョブの名前指定 > サーバー既定(--layout)。
    両方あるとレイアウト名は黙って無視されるため、UIのバグ(古いlayout.jsonが
    別の単語リストのジョブに付く)を後から追えるようWARNINGを残す。
    """
    layout_path = job.dir / LAYOUT_FILENAME
    name = str(job.params.get("layout") or "").strip()
    if layout_path.exists():
        if name:
            logger.warning(
                "[job %s] レイアウト名(%s)と%sの両方が指定されています。"
                "%sを使います(レイアウト名は無視されます)",
                job.id,
                name,
                LAYOUT_FILENAME,
                LAYOUT_FILENAME,
            )
        return str(layout_path), f"json:{LAYOUT_FILENAME}"
    if name:
        return name, f"name:{name}"
    default: str | None = config.get("layout")
    if default:
        return default, f"server-default:{default}"
    return None, "builtin-default"


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


def _run_synthesize(
    job: Job,
    config: dict[str, Any],
    project: Any,
    synthesize,
    octave_keys: list[int] | None = None,
) -> Any:
    """synthesizeステージを実行し、進捗率と残り時間の目安を job に反映する。

    NEUTRINOの進捗出力を job.stage_progress に、過去実績からの所要見積りを
    job.stage_estimated_total に入れる(to_dict がこれらから %/残り秒を出す)。
    成功後は今回の実績を throughput ストアに記録して次回の見積りに使う。
    """
    # 未記録の古いジョブはNEUTRINO時代のものなので neutrino 扱い(見積りの互換のため据え置く)
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
            # 新キー auto_octave 優先。旧ジョブの voicevox_auto_octave も後方互換で読む
            auto_octave=job.params.get(
                "auto_octave", job.params.get("voicevox_auto_octave", True)
            ),
            # プレビューは曲の一部だけを渡すので、自動オクターブ調整の判定には
            # 切り出す前の全音符の音高を使う(本番とキーをそろえる)
            octave_keys=octave_keys,
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
        # 自動削除はTTLが正のときだけ。無効なら従来どおりスレッドも作らない
        self._cleaner: threading.Thread | None = None
        if _env_float(JOB_TTL_HOURS_ENV, 0.0) > 0:
            self._cleaner = threading.Thread(target=self._cleanup_loop, daemon=True)
            self._cleaner.start()

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
                owner=data.get("owner"),
                status=data.get("status", "error"),
                stages=data.get("stages", []),
                error=data.get("error"),
                layout_source=data.get("layout_source"),
            )
            if data.get("created_at"):
                job.created_at = datetime.fromisoformat(data["created_at"]).timestamp()
            job.finished_at = data.get("finished_at")
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
        owner: str | None = None,
        wordlist_csv: str = "",
        wordlist_images: dict[str, bytes] | None = None,
    ) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "input.mid").write_bytes(midi)
        # 自作の単語リスト(正規化済みCSV)はこのジョブの中だけに置く。
        # 名前は params["wordlist_csv"] 側で決まっている(custom_wordlist_name)
        if wordlist_csv:
            wl_dir = job_dir / WORDLIST_DIRNAME
            wl_dir.mkdir(exist_ok=True)
            if wordlist_images:
                wordlist_csv = _store_wordlist_images(wl_dir, wordlist_csv, wordlist_images)
            (wl_dir / str(params["wordlist_csv"])).write_text(
                wordlist_csv, encoding="utf-8"
            )
        if editor:
            (job_dir / "editor.json").write_bytes(editor)
        if lyrics.strip():
            (job_dir / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        if layout_json.strip():
            (job_dir / LAYOUT_FILENAME).write_text(layout_json, encoding="utf-8")
        job = Job(id=job_id, dir=job_dir, params=params, owner=owner)
        with self._lock:
            self.jobs[job_id] = job
        self._save(job)
        self._queue.put(job)
        return job

    def get(self, job_id: str, owner: str | None = None) -> Job:
        """ジョブを引く。ownerを渡すと持ち主が違うジョブは404にする(公開モード)。"""
        job = self.jobs.get(job_id)
        if job is None or (owner is not None and job.owner != owner):
            raise HTTPException(status_code=404, detail="ジョブが見つかりません")
        return job

    def visible_jobs(self, owner: str | None = None) -> list[Job]:
        """一覧に出すジョブ。ownerを渡すとそのセッションのぶんだけ返す(公開モード)。"""
        jobs = list(self.jobs.values())
        if owner is not None:
            jobs = [j for j in jobs if j.owner == owner]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def active_count(self) -> int:
        """待機中+実行中のジョブ数(キュー上限の判定用。ワーカーは1本で全員共用)。"""
        return sum(1 for j in self.jobs.values() if j.status in ("queued", "running"))

    def recent_count(self, owner: str, since: float) -> int:
        """since 以降にこのセッションが投入したジョブ数(日次クォータの判定用)。"""
        return sum(
            1 for j in self.jobs.values() if j.owner == owner and j.created_at >= since
        )

    def _cleanup_loop(self) -> None:
        """完了から一定時間経ったジョブを定期的に消す(公開インスタンスの容量対策)。"""
        while True:
            time.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                self.cleanup_expired()
            except Exception:  # noqa: BLE001 - 掃除の失敗でスレッドを落とさない
                logger.exception("ジョブの自動削除に失敗しました")

    def cleanup_expired(self, now: float | None = None) -> list[str]:
        """SORAMIMIC_JOB_TTL_HOURS を過ぎた完了ジョブを削除し、そのIDを返す。

        TTLが0以下(既定)なら何もしない。実行中・待機中のジョブは対象外。
        """
        hours = _env_float(JOB_TTL_HOURS_ENV, 0.0)
        if hours <= 0:
            return []
        deadline = (now or time.time()) - hours * 3600
        with self._lock:
            expired = [
                job
                for job in self.jobs.values()
                if job.status in ("done", "error", "canceled")
                and (job.finished_at or job.created_at) <= deadline
            ]
            for job in expired:
                self.jobs.pop(job.id, None)
        for job in expired:
            shutil.rmtree(job.dir, ignore_errors=True)
            logger.info("[job %s] 保存期間を過ぎたので削除しました", job.id)
        return [job.id for job in expired]

    def _save(self, job: Job) -> None:
        data = job.to_dict(with_log=False)
        # owner/finished_at はAPIのレスポンス(to_dict)には出さないが、再起動後も
        # 持ち主判定・自動削除ができるよう status.json には残す
        if job.owner:
            data["owner"] = job.owner
        if job.finished_at:
            data["finished_at"] = job.finished_at
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
        # 同じディレクトリの一時ファイルに書いてから置換する。ジョブ実行中も
        # APIスレッドが status.json を読むので、書きかけの中身を読ませない
        status_path = job.dir / STATUS_FILENAME
        tmp_path = status_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp_path, status_path)

    def cancel(self, job_id: str, owner: str | None = None) -> Job:
        job = self.get(job_id, owner)
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
    config: dict[str, Any] = {
        # 単語画像はジョブをまたいで共有する(初回ジョブの動画ステージが
        # 画像ダウンロードで数分かかるため。2回目以降はほぼゼロになる)
        "image_cache": jobs_dir.resolve() / "image-cache",
        # 生成前に出す仮サムネ(/api/thumbnail-preview)のPNGキャッシュ
        "preview_cache": preview_cache_dir(jobs_dir),
        "soundfont": resolve_soundfont(soundfont),
        "font": font or default_font(),
        "threads": threads,
        "layout": layout,
        "voicevox_url": voicevox_url,
        # 合成の所要時間の目安(曲秒あたりの実処理秒)を実行ごとに記録して次回に使う
        "throughput_store": jobs_dir.resolve() / THROUGHPUT_FILENAME,
    }
    manager = JobManager(jobs_dir, config)
    # サムネプレビューの短期レート制限(セッション単位)。ジョブの日次クォータとは別枠
    preview_limiter = RateLimiter()
    # よく使う単語リストの前処理(parse_tidy)は大きいリストだと数分かかる。
    # 指定があればバックグラウンドで先に構築しておき、初回変換も速くする
    start_warmup_thread()
    app = FastAPI(title="soramimic-video API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        # 同梱UIは同一オリジンなので不要だが、別オリジンのUIからも
        # プレビューの状態(絵が間に合ったか)を読めるようにしておく
        expose_headers=["X-Preview-Cache", "X-Preview-Images"],
    )
    app.state.manager = manager

    @app.middleware("http")
    async def _session_cookie(request: Request, call_next):
        """公開モードで匿名セッションID(HttpOnly cookie)を発行・引き回す。

        非公開モードでは何もしない(cookieも発行しない)ので従来と同じ挙動。
        """
        if not is_public_mode():
            return await call_next(request)
        session = request.cookies.get(SESSION_COOKIE) or ""
        issued = not re.fullmatch(r"[0-9a-f]{32}", session)
        if issued:
            session = uuid.uuid4().hex
        request.state.session = session
        response = await call_next(request)
        if issued:
            response.set_cookie(
                SESSION_COOKIE,
                session,
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
        return response

    def owner_of(request: Request) -> str | None:
        """このリクエストのジョブ所有者。非公開モードではNone(=全ジョブ共有)。"""
        if not is_public_mode():
            return None
        # 何かの拍子にセッションが無いときは、誰のジョブにも一致しない値にして
        # 他人のジョブが見えてしまわないようにする(fail-closed)
        return getattr(request.state, "session", None) or "-"

    # editorの静的ビルド(scripts/build-editor.sh の出力)があれば /editor/ で配信する。
    # 無くてもサーバーは起動する(WebUIはeditor連携ボタンを隠すだけ)。
    editor_root = (editor_dist or DEFAULT_EDITOR_DIST).resolve()
    editor_available = (editor_root / "editor.html").is_file()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # 同梱サンプル曲(いずれも詞・曲パブリックドメイン、examples/gen_samples.py で生成)。
    # SORAMIMIC_SAMPLES_DIR を設定するとそのディレクトリのサンプルに差し替わる。
    def _sample_ids() -> set[str]:
        return {str(s["id"]) for s in load_samples() if s.get("id")}

    @app.get("/api/samples")
    def list_samples() -> list[dict[str, Any]]:
        return load_samples()

    # サンプル曲は作り直されることがある(同じURLで中身が変わる)。ブラウザが
    # 古い版を使い回して「更新前の曲」で生成してしまわないよう、毎回問い合わせさせる。
    SAMPLE_CACHE_HEADERS = {"Cache-Control": "no-cache"}

    def _sample_file(sample_id: str, name: str) -> Path:
        """サンプルの付随ファイル。IDが無い・ファイルが欠けていれば404にする。"""
        if sample_id not in _sample_ids():
            raise HTTPException(status_code=404, detail="そのサンプルはありません")
        path = samples_dir() / name
        if not path.is_file():
            # samples.json に載っているのに実ファイルが無い(置き忘れ)。500ではなく
            # 404で返し、UIが「その曲は取れなかった」と扱えるようにする
            logger.warning("サンプルのファイルがありません: %s", path)
            raise HTTPException(
                status_code=404, detail=f"そのサンプルのファイルがありません: {name}"
            )
        return path

    @app.get("/api/sample/{sample_id}/midi")
    def sample_midi(sample_id: str) -> FileResponse:
        return FileResponse(
            _sample_file(sample_id, f"{sample_id}.mid"),
            media_type="audio/midi",
            filename=f"{sample_id}.mid",
            headers=SAMPLE_CACHE_HEADERS,
        )

    @app.get("/api/sample/{sample_id}/lyrics")
    def sample_lyrics(sample_id: str) -> FileResponse:
        return FileResponse(
            _sample_file(sample_id, f"{sample_id}_lyrics.txt"),
            media_type="text/plain",
            headers=SAMPLE_CACHE_HEADERS,
        )

    @app.get("/api/config")
    def get_config(request: Request) -> dict[str, Any]:
        auth_required = bool(os.environ.get(API_KEY_ENV))
        try:
            _require_api_key(request)
        except HTTPException:
            return {"auth_required": True}
        conf: dict[str, Any] = {
            "auth_required": auth_required,
            "models": list_models(),
            "neutrino": bool(os.environ.get("NEUTRINO_ROOT")),
            "voicevox": _voicevox_config(),
            "layouts": builtin_layout_names(),
            # 単語リストを選んだときにUIが既定で当てるレイアウト(wordlist_layouts.json)
            "wordlist_layouts": load_wordlist_layouts(),
            "editor": editor_available,
            # 自作の単語リスト(CSV/zipアップロード)の受け入れ上限
            "max_wordlist_bytes": wordlist_csv_mod.max_bytes(),
            "max_wordlist_rows": wordlist_csv_mod.max_rows(),
            "max_wordlist_zip_bytes": wordlist_zip_mod.max_zip_bytes(),
            "max_wordlist_image_bytes": wordlist_zip_mod.max_image_bytes(),
            "max_wordlist_images": wordlist_zip_mod.max_images(),
        }
        # 公開モードのときだけ、フロントに制限値とクレジット表示の要否を伝える
        if is_public_mode():
            conf["public"] = True
            conf["daily_quota"] = int(_env_float(DAILY_QUOTA_ENV, DEFAULT_DAILY_QUOTA))
            conf["max_song_seconds"] = int(
                _env_float(MAX_SONG_SECONDS_ENV, DEFAULT_MAX_SONG_SECONDS)
            )
        site_key = turnstile_site_key()
        if site_key:
            conf["turnstile_site_key"] = site_key
        return conf

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

    def _sample_title(sample_id: str) -> tuple[str, str]:
        """サンプル曲の (曲名, 読み)。読みは samples.json の title_kana(無ければ空)。

        未知のIDは404。
        """
        entry = sample_entry(sample_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="そのサンプルはありません")
        return str(entry.get("title") or sample_id), str(entry.get("title_kana") or "")

    def _preview_rate_key(request: Request) -> str:
        """レート制限の単位。公開モードは匿名セッション、無ければ接続元IP。"""
        session = getattr(request.state, "session", None)
        if session:
            return f"session:{session}"
        client = request.client.host if request.client else "-"
        return f"ip:{client}"

    @app.get("/api/thumbnail-preview", dependencies=[Depends(_require_api_key)])
    def thumbnail_preview(
        request: Request,
        sample: str = "",
        wordlist: str = "",
        where: str = "",
        convert_params: str = "",
        images: bool = True,
    ) -> FileResponse:
        """生成前に出す仮サムネ(おまかせ確認モーダルのプレビュー)。

        サンプル曲の曲名をその単語リストで1フレーズだけ空耳変換し、実際の
        サムネと同じ描画で小さめのPNG(既定640x360)を返す。結果はディスクに
        キャッシュし、2回目以降は変換せずそのまま返す。
        変換の入力には samples.json の title_kana(曲名の読み)を使う
        (「紅葉」を「コーヨー」と推定させないため)。見出しの曲名は title のまま。

        images=0 なら単語画像を貼らない文字だけのサムネにする。昆虫など画像を
        初期非表示にしている単語リスト(index.html の HIDDEN_PREVIEW_WORDLISTS)
        で、モーダルが「画像を表示する」を押されるまで使う。

        単語画像は数秒だけ待って貼る。間に合わなかったときは文字だけのPNGを
        X-Preview-Images: pending で返し、裏で画像を取り切って同じキャッシュキーを
        絵入りに作り直す。UIは pending を見て数秒後に1回だけ取り直す
        (そのときには作り直し済み=キャッシュヒットなのでレート制限も変換も
        追加で消費しない)。

        ジョブではないので日次クォータは消費しないが、連打で変換が走り続けない
        ようキャッシュミス時だけセッション単位のレート制限をかける(超過は429)。
        UI側は429・エラー・タイムアウトのいずれでも単語リストの代表画像に
        フォールバックするので、ここで失敗してもモーダルの機能は壊れない。
        """
        from .convert import parse_convert_params
        from .thumbnail_preview import PreviewSpec, render_slot

        title, title_kana = _sample_title(sample)
        wordlist = wordlist.strip()
        if not wordlist:
            raise HTTPException(status_code=400, detail="単語リスト名(wordlist)が必要です")
        try:
            spec = PreviewSpec.create(
                title,
                wordlist,
                where=where.strip() or None,
                params=parse_convert_params(convert_params),
                with_images=images,
                title_kana=title_kana,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        cache_dir = config["preview_cache"]
        hit = spec.cached(cache_dir)
        if hit is not None:
            return _preview_response(hit, cached=True, pending=spec.images_pending(cache_dir))
        if not preview_limiter.allow(_preview_rate_key(request)):
            raise HTTPException(
                status_code=429,
                detail="プレビューの作成が続いています。少し待ってからお試しください。",
            )
        try:
            with render_slot():
                # 待っている間に他のリクエストが作っているかもしれない
                hit = spec.cached(cache_dir)
                if hit is not None:
                    return _preview_response(
                        hit, cached=True, pending=spec.images_pending(cache_dir)
                    )
                path = spec.render(cache_dir, image_cache=config["image_cache"])
        except TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="プレビューの作成が混み合っています。少し待ってからお試しください。",
            ) from exc
        if path is None:
            raise HTTPException(status_code=500, detail="プレビューを作成できませんでした")
        return _preview_response(
            path, cached=False, pending=spec.images_pending(cache_dir)
        )

    def _preview_response(path: Path, cached: bool, pending: bool = False) -> FileResponse:
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                # 毎回サーバーに聞く(キャッシュヒットなら数ミリ秒で304/即応答)。
                # 画像の裏読みが間に合って作り直されたとき、ブラウザが古い
                # 「絵なし」プレビューを握り続けないようにする
                "Cache-Control": "private, no-cache",
                "X-Preview-Cache": "hit" if cached else "miss",
                # 単語画像が間に合わず文字だけで返したときは pending。UIはこれを見て
                # 数秒後に1回だけ取り直す(裏で絵入りに作り直されているのでヒットする)
                "X-Preview-Images": "pending" if pending else "ready",
            },
        )

    @app.post("/api/editor-preview", dependencies=[Depends(_require_api_key)])
    async def editor_preview(
        editor: UploadFile,
        wordlist: str = Form(""),
        cue: int = Form(0),
        layout_json: str = Form(""),
        lyrics: str = Form(""),
        subtitle_granularity: str = Form(""),
    ) -> dict[str, Any]:
        """editor書き出しJSONの変換結果に基づく、キュー1枚ぶんのプレビューデータ。

        レイアウト編集画面のプレビューを、単語リストの代表行1件ではなく実際の
        変換結果(replaced単語列)で描くための元データ。cueで動画のキュー順に送る。
        """
        from .align import parse_granularity_override
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
                payload, wordlist.strip() or None, layout_obj, lyrics,
                parse_granularity_override(subtitle_granularity),
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

    def _check_turnstile(request: Request, token: str) -> None:
        """Turnstileが設定されていればトークンを検証する(未設定なら何もしない)。"""
        if not os.environ.get(TURNSTILE_SECRET_ENV, "").strip():
            return
        client_ip = request.client.host if request.client else None
        if not verify_turnstile(token, client_ip):
            raise HTTPException(
                status_code=403,
                detail="人間かどうかの確認に失敗しました。"
                "ページを再読み込みしてもう一度お試しください。",
            )

    def _check_public_limits(owner: str | None, midi_bytes: bytes) -> None:
        """公開モードの投入制限(キュー上限・日次クォータ・曲長)をまとめて確認する。

        非公開モードでは何もしない。超過は429(混雑・クォータ)か400(曲長)。
        """
        if not is_public_mode():
            return
        queue_limit = int(_env_float(QUEUE_LIMIT_ENV, DEFAULT_QUEUE_LIMIT))
        if queue_limit > 0 and manager.active_count() >= queue_limit:
            raise HTTPException(
                status_code=429,
                detail=f"順番待ちが混み合っています(同時に{queue_limit}件まで)。"
                "しばらく待ってからもう一度お試しください。",
            )
        quota = int(_env_float(DAILY_QUOTA_ENV, DEFAULT_DAILY_QUOTA))
        if owner and quota > 0:
            used = manager.recent_count(owner, time.time() - 24 * 3600)
            if used >= quota:
                raise HTTPException(
                    status_code=429,
                    detail=f"1日に作れる本数の上限({quota}本)に達しました。"
                    "24時間ほど空けてからまたお試しください。",
                )
        max_seconds = _env_float(MAX_SONG_SECONDS_ENV, DEFAULT_MAX_SONG_SECONDS)
        if max_seconds > 0:
            seconds = song_seconds(midi_bytes)
            if seconds is not None and seconds > max_seconds:
                raise HTTPException(
                    status_code=400,
                    detail=f"曲が長すぎます(この曲は{fmt_duration_ja(seconds)}、"
                    f"上限は{fmt_duration_ja(max_seconds)}です)。"
                    "もっと短い曲でお試しください。",
                )

    @app.post("/api/jobs", dependencies=[Depends(_require_api_key)])
    async def create_job(
        request: Request,
        midi: UploadFile,
        editor: UploadFile | None = None,
        # 自作の単語リスト(CSV)。付いていればリスト名より優先する
        wordlist_csv: UploadFile | None = None,
        # 画面に貼り付けた単語リスト(zipを作らずに画像を付ける経路)。
        # wordlist_csv が付いていないときだけ見る。画像は名前で行に結びつく
        wordlist_text: str = Form(""),
        wordlist_images: list[UploadFile] = File(default_factory=list),
        wordlist_name: str = Form(""),
        lyrics: str = Form(""),
        model: str = Form("MERROW"),
        # 省略時はどのサーバーでも通るVOICEVOXにする(NEUTRINOはNEUTRINO_ROOT
        # 未設定のサーバーだと下の422ゲートで弾かれてしまうため既定にしない)
        synthesizer: str = Form("voicevox"),
        voicevox_style: int = Form(3003),
        auto_octave: bool | None = Form(None),
        # 旧名。auto_octave に統合したが後方互換で受け続ける(deprecated)。
        # 新旧両方来たら新名(auto_octave)を優先する。
        voicevox_auto_octave: bool | None = Form(None),
        transpose: int = Form(0),
        preview: float = Form(0),
        # プレビューで切り出す場所。head(既定=歌い出し) / high(最高音を含む
        # フレーズ) / low(最低音を含むフレーズ)。不正値は head 扱い
        preview_mode: str = Form(""),
        # サムネ・表示用の曲名。WebUIはサンプル曲なら samples.json の title、
        # 自分のMIDIならファイル名(拡張子なし)を送る。未指定なら midi_filename を使う
        song_title: str = Form(""),
        wordlist: str = Form(""),
        where: str = Form(""),
        convert_params: str = Form(""),
        layout: str = Form(""),
        layout_json: str = Form(""),
        subtitle_granularity: str = Form(""),
        # Cloudflare Turnstile(TURNSTILE_SECRET_KEY 設定時のみ検証する)
        turnstile_token: str = Form(""),
    ) -> dict[str, Any]:
        _check_turnstile(request, turnstile_token)
        midi_bytes = await midi.read()
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        owner = owner_of(request)
        _check_public_limits(owner, midi_bytes)
        editor_bytes = None
        editor_payload: Any = None
        if editor is not None and editor.filename:
            editor_bytes = await editor.read()
            try:
                editor_payload = json.loads(editor_bytes)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail="editorのJSONが読めません"
                ) from exc
        # 自作の単語リスト(CSV/画像入りzip、または貼り付けテキスト+画像)。
        # ジョブを走らせる前にここで検証して弾く
        custom: wordlist_zip_mod.WordlistZip | None = None
        has_wordlist_file = wordlist_csv is not None and bool(wordlist_csv.filename)
        try:
            if has_wordlist_file and wordlist_csv is not None:
                custom = wordlist_zip_mod.parse_upload(await wordlist_csv.read())
            elif wordlist_text.strip():
                custom = wordlist_zip_mod.parse_parts(
                    wordlist_text.encode("utf-8"),
                    await _read_wordlist_images(wordlist_images),
                )
        except wordlist_csv_mod.WordlistCsvError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # プレビューは元歌詞をそのまま歌わせるので替え歌の入力は不要
        if preview <= 0 and editor_bytes is None and custom is None and not wordlist.strip():
            raise HTTPException(
                status_code=422,
                detail="editorの書き出しJSONか単語リスト(名前かCSV)のどちらかが必要です",
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
        # NEUTRINO未設定のサーバー(公開インスタンスなど)は合成の途中で必ず落ちる。
        # 走らせてから失敗させず、受付時に理由を返す(UI側も選択肢を無効化している)
        if synthesizer == "neutrino" and not os.environ.get("NEUTRINO_ROOT"):
            raise HTTPException(
                status_code=422,
                detail="このサーバーではNEUTRINOを使えません(synthesizerは voicevox です)",
            )
        # 新名 auto_octave を優先し、無ければ旧名、どちらも無ければ既定True(自動調整ON)
        if auto_octave is None:
            auto_octave = (
                voicevox_auto_octave if voicevox_auto_octave is not None else True
            )
        wordlist = wordlist.strip()
        # editor経由のジョブはJSON側の単語リスト指定がフォーム選択より優先される。
        # 履歴に実際の単語リスト名が残るよう、ここで解決して params に入れる
        if isinstance(editor_payload, dict):
            from .editor_io import _resolve_preview_wordlist

            resolved = _resolve_preview_wordlist(editor_payload, wordlist or None)
            if resolved:
                wordlist = Path(resolved).stem if resolved.endswith(".csv") else resolved
        params = {
            "model": model.strip() or "MERROW",
            "synthesizer": synthesizer,
            "voicevox_style": voicevox_style,
            "auto_octave": auto_octave,
            "transpose": transpose,
            "preview": max(0.0, min(preview, 60.0)),
            "preview_mode": (
                preview_mode.strip() if preview_mode.strip() in PREVIEW_MODES else ""
            ),
            "wordlist": wordlist,
            "where": where.strip(),
            "convert_params": convert_params.strip(),
            "layout": layout,
            "subtitle_granularity": subtitle_granularity.strip(),
            "parody_source": "editor" if editor_bytes else "convert",
            "midi_filename": midi.filename,
            "song_title": song_title.strip(),
        }
        if custom is not None:
            # 表示名(履歴・サムネ・ダウンロード名)はアップロードしたファイル名から作る
            # (貼り付けテキストならフォームのリスト名。どちらも空なら "custom")。
            # 中身が変われば指紋も変わるので、来歴の突き合わせにも使える
            name = custom_wordlist_name(
                (wordlist_csv.filename if has_wordlist_file and wordlist_csv else "")
                or f"{wordlist_name.strip()}.csv"
            )
            params["wordlist"] = name
            params["wordlist_csv"] = f"{name}.csv"
            params["wordlist_fingerprint"] = custom.csv.fingerprint
            params["wordlist_rows"] = custom.csv.rows
            if custom.image_count:
                params["wordlist_images"] = custom.image_count
            # 自作リストは絞り込み(where)の対象になる列が無いので付けない
            params["where"] = ""
        job = manager.create(
            midi_bytes, editor_bytes, lyrics, params,
            layout_json=layout_json, owner=owner,
            wordlist_csv=custom.csv.text if custom is not None else "",
            wordlist_images=custom.images if custom is not None else None,
        )
        return {"id": job.id}

    @app.post("/api/wordlist-check", dependencies=[Depends(_require_api_key)])
    async def wordlist_check(
        wordlist_csv: UploadFile | None = None,
        wordlist_text: str = Form(""),
        wordlist_images: list[UploadFile] = File(default_factory=list),
        wordlist_name: str = Form(""),
    ) -> dict[str, Any]:
        """自作の単語リストを投入前に検査する。

        入力は2通り。アップロードした1ファイル(CSV、または画像入りzip)か、
        画面に貼り付けたテキスト+別々に選んだ画像(wordlist_text/wordlist_images)。
        どちらか一方だけを受け取る(両方あると、どちらを使ったか画面と食い違う)。

        列・行数・読みの書き方、画像があればその中身まで見て、駄目なら400で理由を返す。
        通れば「何語読めたか(と画像が何枚付いたか)」をUIに返して、ジョブを投げる前に
        確認できるようにする(/api/midi-check と同じ流儀)。ここではファイルを保存しない。
        """
        has_file = wordlist_csv is not None and bool(wordlist_csv.filename)
        has_text = bool(wordlist_text.strip())
        if has_file and has_text:
            raise HTTPException(
                status_code=400,
                detail="単語リストはファイルか書いた内容のどちらか一方にしてください。",
            )
        if not has_file and not has_text:
            raise HTTPException(
                status_code=400,
                detail="単語リストがありません。ファイルを選ぶか、単語を書いてください。",
            )
        try:
            if has_file and wordlist_csv is not None:
                parsed = wordlist_zip_mod.parse_upload(await wordlist_csv.read())
                name = custom_wordlist_name(wordlist_csv.filename or "")
            else:
                parsed = wordlist_zip_mod.parse_parts(
                    wordlist_text.encode("utf-8"),
                    await _read_wordlist_images(wordlist_images),
                )
                # リスト名は任意。空なら custom_wordlist_name の既定("custom")に落ちる
                name = custom_wordlist_name(f"{wordlist_name.strip()}.csv")
        except wordlist_csv_mod.WordlistCsvError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**parsed.summary(), "name": name}

    @app.get("/api/jobs", dependencies=[Depends(_require_api_key)])
    def list_jobs(request: Request) -> list[dict[str, Any]]:
        jobs = manager.visible_jobs(owner_of(request))
        return [j.to_dict(with_log=False) for j in jobs[:30]]

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        return manager.get(job_id, owner_of(request)).to_dict()

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(_require_api_key)])
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        return manager.cancel(job_id, owner_of(request)).to_dict(with_log=False)

    @app.get("/api/jobs/{job_id}/video", dependencies=[Depends(_require_api_key)])
    def get_video(job_id: str, request: Request) -> FileResponse:
        job = manager.get(job_id, owner_of(request))
        if job.status != "done" or not job.video or not job.video.exists():
            raise HTTPException(status_code=409, detail="動画はまだできていません")
        if job.video.suffix == ".wav":  # プレビュー(歌声のみ)
            return FileResponse(
                job.video, media_type="audio/wav", filename=_download_filename(job)
            )
        return FileResponse(
            job.video, media_type="video/mp4", filename=_download_filename(job)
        )

    @app.get("/api/jobs/{job_id}/thumbnail", dependencies=[Depends(_require_api_key)])
    def get_thumbnail(job_id: str, request: Request) -> FileResponse:
        """サムネ画像(video ステージが作る thumbnail.png)。未生成なら404。"""
        job = manager.get(job_id, owner_of(request))
        if not job.thumbnail.exists():
            raise HTTPException(status_code=404, detail="サムネ画像がありません")
        return FileResponse(
            job.thumbnail, media_type="image/png", filename=_thumbnail_filename(job)
        )

    # ---- 同梱editor(/editor/)向けの配信・シード(A-2) ----
    # 以下のルートは StaticFiles マウントより前に登録して優先させる
    # (単語リストは submodule内のダミーではなく external/soramimic-wordlists の
    #  正データを、confは dist のスナップショットではなくソース側を、
    #  kuromoji辞書は Content-Encoding を付けず素のバイナリで返す)。

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

    @app.get("/editor/conf/setting.json")
    def editor_setting_json() -> FileResponse:
        """editorのconf(setting.json)をソース側の正データから返す。

        dist側の conf はビルド時にコピーされたスナップショットで古いことが
        あり、後から追加された単語リスト(youtuber等)が選択肢に出ない。
        external/soramimic/conf/setting.json を優先し、無ければ dist の
        conf にフォールバックする。
        """
        from .editor_io import SETTING_JSON

        path = SETTING_JSON
        if not path.is_file():
            path = editor_root / "conf" / "setting.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="設定が見つかりません")
        return FileResponse(path, media_type="application/json")

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

    @app.post("/api/midi-check", dependencies=[Depends(_require_api_key)])
    async def midi_check(midi: UploadFile, lyrics: str = Form("")) -> dict[str, Any]:
        """選ばれたMIDIに歌詞が入っているかを、生成に進む前にその場で調べる。

        この画面のパイプラインは XF MIDI の歌詞(XFKMチャンク)を歌唱・空耳変換の
        入力にしているので、歌詞の無いMIDIは何分も待たせた末にジョブが落ちる。
        UIがファイル選択の直後にこれを呼び、歌詞が無ければその場で断れるようにする。

        lyrics(元歌詞テキスト)を一緒に渡すと、字幕と同じ割り付け(align_lines)を
        試して「元歌詞が対応づかなかったXF行」の数も返す。UIはこれを使って
        元歌詞とMIDIの食い違いを警告する(生成はブロックしない)。

        解析できないMIDI(歌詞なし・XFKMなし・壊れている)はエラーではなく
        has_lyrics=false の判定結果として返す。UIが理由をそのまま出せるように。
        MIDIですらないファイルだけ400。
        """
        import tempfile

        from .align import align_lines
        from .xfparse import analyze_midi

        midi_bytes = await midi.read()
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        lyric_lines = [ln.strip() for ln in lyrics.splitlines()]
        lyric_lines = [ln for ln in lyric_lines if ln]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.mid"
            path.write_bytes(midi_bytes)
            try:
                project = analyze_midi(path)
            except Exception as exc:  # noqa: BLE001 - 歌詞なしMIDIは判定結果として返す
                logger.info("MIDIの歌詞チェックで解析に失敗しました: %s", exc)
                return {
                    "has_lyrics": False,
                    "lines": 0,
                    "lyrics_lines": len(lyric_lines),
                    "unmatched_lines": 0,
                    "detail": str(exc),
                }
            if lyric_lines:
                align_lines(project, lyric_lines)
        unmatched = sum(1 for ln in project.lines if not ln.original_text)
        return {
            "has_lyrics": bool(project.lines),
            "lines": len(project.lines),
            "lyrics_lines": len(lyric_lines),
            # 元歌詞を渡していないときは全行が「対応なし」になるので0で返す
            "unmatched_lines": unmatched if lyric_lines else 0,
            "detail": "",
        }

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
