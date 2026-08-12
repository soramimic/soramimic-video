"""動画生成APIサーバー(ローカル/自宅サーバー向け)。

POST /api/jobs にXF MIDI(+soramimic editorの書き出しJSON、元歌詞)を投げると
analyze → import-editor(またはconvert) の後、歌声合成+mixと無音動画生成を
並列実行し、最後に音声を結合する。進捗は GET /api/jobs/{id}、完成動画は
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

import copy
import csv
import hashlib
import hmac
import ipaddress
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import runproc, synth_estimate
from . import wordlist_csv as wordlist_csv_mod
from . import wordlist_zip as wordlist_zip_mod
from .access_identity import canonical_email, valid_issuer, verify_access_email
from .layout import (
    LAYOUTS_DIR,
    builtin_layout_names,
    load_layout,
    load_wordlist_layouts,
    parse_layout,
)
from .soramimic_engine import start_warmup_thread
from .thumbnail_preview import RateLimiter, preview_cache_dir

if TYPE_CHECKING:  # 型注釈だけ。実行時のimportはハンドラの中で行う(起動を軽く保つ)
    from .project import Project

logger = logging.getLogger(__name__)

API_KEY_ENV = "SORAMIMIC_VIDEO_API_KEY"
# ---- 公開モード(一般公開インスタンス)向けの環境変数 ----
# いずれも未設定なら従来どおりの挙動(制限なし・ジョブは全員から見える)。
PUBLIC_ENV = "SORAMIMIC_PUBLIC"  # 1/true で公開モード
SIMPLE_UI_ENV = "SORAMIMIC_SIMPLE_UI"  # 初回公開用の選択肢を絞ったUI
QUEUE_LIMIT_ENV = "SORAMIMIC_QUEUE_LIMIT"  # 待機+実行中ジョブの上限
DAILY_QUOTA_ENV = "SORAMIMIC_DAILY_QUOTA"  # セッションあたり24時間の投入上限
IP_DAILY_QUOTA_ENV = "SORAMIMIC_IP_DAILY_QUOTA"
IP_HASH_KEY_ENV = "SORAMIMIC_IP_HASH_KEY"
MAX_SONG_SECONDS_ENV = "SORAMIMIC_MAX_SONG_SECONDS"  # 入力MIDIの演奏時間の上限(秒)
JOB_TTL_HOURS_ENV = "SORAMIMIC_JOB_TTL_HOURS"  # 完了後に自動削除するまでの時間(0=無効)
SAMPLES_DIR_ENV = "SORAMIMIC_SAMPLES_DIR"  # 同梱サンプル曲の差し替え先
LOCAL_SAMPLES_MANIFEST = "samples.local.json"  # ローカル限定サンプルの追加分(非追跡)
LAUNCH_CATALOG_ENV = "SORAMIMIC_LAUNCH_CATALOG"  # 環境別の公開選択肢(非追跡可)
TURNSTILE_SECRET_ENV = "TURNSTILE_SECRET_KEY"  # Cloudflare Turnstileの秘密鍵
TURNSTILE_SITE_ENV = "TURNSTILE_SITE_KEY"  # 同・サイトキー(フロントに渡す)
OPS_TOKEN_ENV = "SORAMIMIC_OPS_TOKEN"
EXPOSE_OPS_ENV = "SORAMIMIC_EXPOSE_OPS"
ALLOW_LOCAL_OPS_ENV = "SORAMIMIC_ALLOW_LOCAL_OPS"
TRUSTED_PROXY_IPS_ENV = "SORAMIMIC_TRUSTED_PROXY_IPS"
QUOTA_EXEMPT_EMAILS_ENV = "SORAMIMIC_QUOTA_EXEMPT_EMAILS"
CF_ACCESS_TEAM_DOMAIN_ENV = "SORAMIMIC_CF_ACCESS_TEAM_DOMAIN"
CF_ACCESS_AUD_ENV = "SORAMIMIC_CF_ACCESS_AUD"
GET_RATE_LIMIT_ENV = "SORAMIMIC_GET_RATE_LIMIT"
GET_RATE_WINDOW_ENV = "SORAMIMIC_GET_RATE_WINDOW"
GET_IP_RATE_LIMIT_ENV = "SORAMIMIC_GET_IP_RATE_LIMIT"
GET_IP_RATE_WINDOW_ENV = "SORAMIMIC_GET_IP_RATE_WINDOW"
GET_CACHE_HIT_RATE_LIMIT_ENV = "SORAMIMIC_GET_CACHE_HIT_RATE_LIMIT"
GET_CACHE_HIT_IP_RATE_LIMIT_ENV = "SORAMIMIC_GET_CACHE_HIT_IP_RATE_LIMIT"
DEFAULT_GET_RATE_LIMIT = 15
DEFAULT_GET_IP_RATE_LIMIT = 90  # NAT配下の複数利用者を巻き込みにくいバックストップ
DEFAULT_GET_CACHE_HIT_RATE_LIMIT = 120
DEFAULT_GET_CACHE_HIT_IP_RATE_LIMIT = 600
DEFAULT_GET_RATE_WINDOW = 60.0
GET_CONCURRENCY = 4
SIMPLE_MAX_REQUEST_BYTES_ENV = "SORAMIMIC_SIMPLE_MAX_REQUEST_BYTES"
DEFAULT_SIMPLE_MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_QUEUE_LIMIT = 5
DEFAULT_DAILY_QUOTA = 5
DEFAULT_IP_DAILY_QUOTA = 30
DEFAULT_MAX_SONG_SECONDS = 420.0
SESSION_COOKIE = "sv_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30日
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
CLEANUP_INTERVAL_SECONDS = 3600  # ジョブ自動削除の巡回間隔
STATIC_DIR = Path(__file__).parent / "static"
LAUNCH_CATALOG_PATH = STATIC_DIR / "launch_catalog.json"
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


def is_simple_ui() -> bool:
    """初回公開用の簡易UIかどうか。

    公開モードと分けてあるのは、手元・既存の公開サーバーで
    従来の全機能UIをそのまま使えるようにするため。
    """
    return os.environ.get(SIMPLE_UI_ENV, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def load_launch_catalog() -> dict[str, Any]:
    """初回公開で見せる曲・単語リストと固定歌声を読む。

    ``SORAMIMIC_LAUNCH_CATALOG`` を使うと、権利確認済みだが公開repositoryへ
    再配布しない素材などを環境ごとに選べる。素材本体と同様、release外の永続pathを
    指定することを想定している。
    """
    override = os.environ.get(LAUNCH_CATALOG_ENV, "").strip()
    path = Path(override).expanduser() if override else LAUNCH_CATALOG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"初回公開カタログが読めません ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("初回公開カタログはJSON objectで指定してください")
    return data


def launch_sample_ids() -> set[str]:
    """Simple UIで公開するサンプルID。ファイルパスとして安全なIDだけを返す。"""
    return {
        str(value)
        for value in load_launch_catalog().get("samples", [])
        if re.fullmatch(r"[A-Za-z0-9_-]+", str(value))
    }


def launch_wordlist_names() -> set[str]:
    """Simple UIの全APIで共有する公開単語リストallowlist。"""
    return {
        str(value)
        for value in load_launch_catalog().get("wordlists", [])
        if re.fullmatch(r"[A-Za-z0-9_-]+", str(value))
    }


def require_launch_wordlist(wordlist: str, *, status_code: int = 404) -> str:
    """Simple UIではカタログ名だけを許可し、filesystem pathを解決前に拒否する。"""
    name = wordlist.strip()
    if is_simple_ui() and name not in launch_wordlist_names():
        raise HTTPException(
            status_code=status_code,
            detail="この単語リストは現在利用できません",
        )
    return name


def require_launch_midi(filename: str | None, data: bytes) -> str | None:
    """Simple UIのMIDIをカタログ同梱ファイルの名前とSHA-256で照合する。"""
    if not is_simple_ui():
        return None
    supplied_name = filename or ""
    supplied_digest = hashlib.sha256(data).digest()
    for sample_id in sorted(launch_sample_ids()):
        expected_name = f"{sample_id}.mid"
        if supplied_name != expected_name:
            continue
        path = samples_dir() / expected_name
        try:
            expected_digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            break
        if secrets.compare_digest(supplied_digest, expected_digest):
            return sample_id
    raise HTTPException(
        status_code=422,
        detail="このMIDIファイルは現在利用できません",
    )


async def read_midi_upload(midi: UploadFile) -> bytes:
    """Simple UIでは最大の同梱MIDIを超えた時点で読み止め、巨大入力を保持しない。"""
    if not is_simple_ui():
        return await midi.read()
    sizes: list[int] = []
    for sample_id in launch_sample_ids():
        try:
            sizes.append((samples_dir() / f"{sample_id}.mid").stat().st_size)
        except OSError:
            continue
    return await midi.read(max(sizes, default=0) + 1)


def require_launch_lyrics(sample_id: str | None, lyrics: str) -> str:
    """Simple UIでは照合済みMIDIに付属する元歌詞を常に使う。"""
    if not is_simple_ui():
        return lyrics
    if not sample_id:
        raise HTTPException(status_code=422, detail="選択した曲の歌詞を確認できません")
    try:
        return (samples_dir() / f"{sample_id}_lyrics.txt").read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=422, detail="選択した曲の歌詞が見つかりません") from exc


def visible_samples() -> list[dict[str, Any]]:
    """UIとサンプル取得APIに出す曲。簡易UIでは順序も固定する。"""
    samples = load_samples()
    if not is_simple_ui():
        return samples
    by_id = {str(row.get("id")): row for row in samples if row.get("id")}
    wanted = load_launch_catalog().get("samples", [])
    return [by_id[str(sample_id)] for sample_id in wanted if str(sample_id) in by_id]


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
    """samples.json とローカル限定の追加manifestを読む。

    ``samples.local.json`` は ``.gitignore`` 対象で、手元の権利曲などを
    公開manifestへ混ぜずにWeb UI/APIへ追加するためのオーバーレイ。無ければ
    従来どおり ``samples.json`` だけを返す。
    """
    directory = samples_dir()
    try:
        raw = (directory / "samples.json").read_text(encoding="utf-8")
    except OSError:
        logger.warning("samples.json を読めません: %s", directory)
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("samples.json が壊れています: %s", directory)
        return []
    base = [e for e in entries if isinstance(e, dict)]

    local_path = directory / LOCAL_SAMPLES_MANIFEST
    try:
        local_raw = local_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return base
    except OSError:
        logger.warning("ローカルサンプルmanifestを読めません: %s", local_path)
        return base
    try:
        local_entries = json.loads(local_raw)
    except json.JSONDecodeError:
        logger.warning("ローカルサンプルmanifestが壊れています: %s", local_path)
        return base

    merged = list(base)
    positions = {
        str(entry["id"]): i
        for i, entry in enumerate(merged)
        if entry.get("id")
    }
    for entry in local_entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        sample_id = str(entry["id"])
        if sample_id in positions:
            merged[positions[sample_id]] = {**merged[positions[sample_id]], **entry}
        else:
            positions[sample_id] = len(merged)
            merged.append(entry)
    return merged


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
    # 公開モードの日次IP枠だけに使う短いHMAC。接続元IPそのものは保存しない。
    # Accessで免除されたジョブはNoneのままにしてIP枠を消費させない。
    client_hash: str | None = None
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
            # 公開APIへ例外本文を返すとparser/ffmpeg由来の内部pathやコマンドが漏れる。
            "error": "生成に失敗しました" if is_public_mode() and self.error else self.error,
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
        if with_log and not is_public_mode():
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


def store_editor_session_wordlist(
    sessions_dir: Path, custom: wordlist_zip_mod.WordlistZip
) -> str:
    """自作リストをeditorセッションとして置き、そのセッションID(sid)を返す。

    置き場は <jobs_dir>/editor-sessions/<sid>/wordlist.csv。sid は正規化済み
    CSVの内容ハッシュ(fingerprint)なので、同じリストで何度エディタを開いても
    同じディレクトリを指す(=決定的。書き直しても中身は同一になる)。
    画像はジョブと同じ流儀で <sid>/images/ に置き、CSVの image 列を実体の
    絶対パスに差し替える(動画生成側の download_image がローカルパスとして拾う)。
    """
    from .editor_io import SESSION_WORDLIST_FILENAME

    sid = custom.csv.fingerprint
    session_dir = Path(sessions_dir) / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    text = custom.csv.text
    if custom.images:
        text = _store_wordlist_images(session_dir, text, custom.images)
    (session_dir / SESSION_WORDLIST_FILENAME).write_text(text, encoding="utf-8")
    return sid


def editor_setup_seed(
    project: Project,
    wordlist: str,
    where: str | None,
    params: dict[str, str],
    wordlist_entry: dict[str, Any] | None = None,
    song_title: str = "",
) -> dict[str, Any]:
    """変換せずに解析だけした editor シード(セットアップ画面から始まる形)。

    editor は ``results`` が無いシードを「未変換」とみなし、セットアップ画面
    (曲・単語リスト・変換のしかた)から起動して、ブラウザ内で変換してから
    編集画面に入る(soramimic frontend/README)。ここで返すのはその材料だけ:

    - ``phrases``   … 行ごとの読みカナ(convert_project がエンジンへ渡すのと同じ)
    - ``wordlist``  … 初期選択の単語リスト設定(conf/setting.json と同じ形)。
      絞り込み(where)はこのエントリに載せる(export_editor も同じ)
    - ``where``     … トップレベルの絞り込み。editor はこれをファセットの
      チェック状態の復元(restoreFacets)に使う。復元は convertControls.js の
      ``facetClause`` が作る ``(type=family)`` の形の断片を探す照合なので、
      **その形で表せる where のときだけ**載せる(:mod:`.facets` の
      ``survives_editor_facets``)。表せない where を渡すと全チェックが外れ、
      editor が組み直す where が空になって**絞り込みが消える**——送った条件より
      広くなるので載せない(エントリの where だけが残り、editor は conf の
      既定チェックで始まる)
    - ``param``     … 初期パラメータ(エンジン既定を埋めた実効値。UIが逆算する)
    - ``song``      … 表示用の曲名
    - ``noteLengthRawList`` … 曲のノート長から作るα=1の生重み
    - ``noteLengthAlpha`` … soramimic側UIの初期値。既定0.25
    - ``weightsList`` … 旧soramimic互換の計算済み重み

    ``results``/``tokensList``/``unitsList`` は入れない——ブラウザ側で導出される。

    wordlist_entry を渡すと単語リスト設定にそのまま使う(自作リスト用。
    export_editor と同じ約束)。渡さないときは wordlist 名から組み立て、
    リスト名も空(=解析のみモードで単語リスト未指定)ならフィールドごと省く
    ——editor はそのとき conf/setting.json の既定(active)で始まる。
    """
    from .convert import (
        engine_phrases,
        project_note_length_weights,
        resolve_convert_settings,
        resolve_wordlist,
    )
    from .editor_io import EXPORT_FORMAT, named_wordlist_entry
    from .facets import survives_editor_facets
    from .soramimic_engine import run_tokenize

    csv_path = resolve_wordlist(wordlist) if wordlist else None
    eff_where, coerced, alpha = resolve_convert_settings(csv_path, where, params)
    phrases = engine_phrases(project)
    payload: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "phrases": phrases,
        "param": coerced,
    }
    if wordlist_entry is None and csv_path is not None:
        wordlist_entry = named_wordlist_entry(csv_path.stem, eff_where)
    if wordlist_entry is not None:
        payload["wordlist"] = wordlist_entry
        if survives_editor_facets(wordlist_entry, eff_where):
            payload["where"] = eff_where
    if song_title.strip():
        payload["song"] = {"title": song_title.strip()}
    # 重みの計算にはエンジンが使うのと同じユニット列が要る。単語DBは要らないので
    # トークナイズだけを走らせる(変換はブラウザ側がやる)。ホストはα=1の生重みを
    # 渡し、αの設定と指数計算はsoramimic側が担当する。
    units = run_tokenize(phrases, coerced)
    raw_weights = project_note_length_weights(project, 1.0)(units)
    note_alpha = alpha if "NOTE_LENGTH_WEIGHT" in (params or {}) else 0.25
    if raw_weights is not None:
        payload["noteLengthRawList"] = raw_weights
        payload["noteLengthAlpha"] = note_alpha
        # 更新前のsoramimicにも従来どおり効く互換フィールド。
        if note_alpha > 0:
            payload["weightsList"] = [
                [raw**note_alpha if raw > 0 else 0.0 for raw in row]
                for row in raw_weights
            ]
    return payload


def _csv_without_image_column(text: str) -> str:
    """CSVから image 列を落とす(ブラウザへ返す用)。

    セッションのCSVの image 列にはサーバー上の絶対パスが入っている。editor は
    id/surface/original/pronunciation しか使わないので、パスは返さない。
    正規化済みCSVはクオート無しなので、_store_wordlist_images と同じく
    素の split(",") で扱う。末尾に改行は付けない(editorのパーサが落ちるため)。
    """
    lines = text.split("\n")
    header = lines[0].split(",") if lines else []
    if "image" not in header:
        return text
    i = header.index("image")

    def drop(cells: list[str]) -> str:
        return ",".join(cells[:i] + cells[i + 1 :]) if i < len(cells) else ",".join(cells)

    return "\n".join(drop(line.split(",")) for line in lines)


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


def _sample_entry_of(params: dict[str, Any]) -> dict[str, Any] | None:
    """ジョブが同梱サンプル曲ならmanifestの1件。自作MIDIならNone。"""
    stem = re.sub(r"\.[^.]*$", "", str(params.get("midi_filename") or "")).strip()
    entry = sample_entry(stem) if stem else None
    if entry is None:
        return None
    title = str(params.get("song_title") or "").strip()
    if title and title != str(entry.get("title") or ""):
        return None
    return entry


def song_title_kana_of(params: dict[str, Any]) -> str:
    """サムネの曲名変換に使う読み(カタカナ)。分からなければ空文字。

    読みが確定しているのは同梱サンプル曲だけ(samples.json の title_kana)。
    自分のMIDIを上げた人がたまたま同じファイル名を付けている場合は、
    song_title がmanifestと一致しないためサンプル扱いしない。
    """
    entry = _sample_entry_of(params)
    if entry is None:
        return ""
    return str(entry.get("title_kana") or "")


def original_credit_of(params: dict[str, Any]) -> str:
    """元曲クレジット。既知のサンプル曲はmanifestの指定を必ず使う。"""
    entry = _sample_entry_of(params)
    if entry is not None and entry.get("original_credit"):
        return str(entry["original_credit"]).strip()
    return str(params.get("original_credit") or "").strip()


def credit_notice_of(params: dict[str, Any]) -> str:
    """権利者・ライセンス指定表記。既知のサンプル曲はmanifestを優先する。"""
    entry = _sample_entry_of(params)
    if entry is not None and entry.get("credit_notice"):
        return str(entry["credit_notice"]).strip()
    return str(params.get("credit_notice") or "").strip()


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
    from .video import (
        actual_video_total_sec,
        attach_audio,
        encode_silent_video,
        make_video,
        planned_video_total_sec,
        prepare_video,
    )
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
            # 自作リストで作った替え歌は、単語リスト行(=単語画像)を
            # editorセッションのCSVから引く。
            # 字幕の元歌詞は analyze 段の align_lines が埋めたものをそのまま使う
            # (lyrics.txt を送っていないジョブだけ editor.json の lyrics で補う)
            import_editor(
                project, d, d / "editor.json",
                sessions_dir=config.get("editor_sessions"),
            )
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

    layout, job.layout_source = resolve_layout(job, config)
    from .align import parse_granularity_override

    video_options: dict[str, Any] = {
        "font": config.get("font") or default_font(),
        "image_cache": config.get("image_cache"),
        "layout": layout,
        "granularity": parse_granularity_override(
            job.params.get("subtitle_granularity")
        ),
        "song_title": song_title_of(job.params),
        "song_title_kana": song_title_kana_of(job.params),
        "synth_credit": synth_credit_of(job.params, config),
        "fps": config.get("video_fps", 30),
        "image_lead_sec": config.get("video_image_lead_sec", 0.1),
        "original_credit": original_credit_of(job.params),
        "credit_notice": credit_notice_of(job.params),
    }

    if not config.get("parallel_video", True):
        _run_synthesize(job, config, project, synthesize)
        project.save(d)
        with _stage(job, "mix"):
            mix(project, d, soundfont=config.get("soundfont"))
        with _stage(job, "video"):
            return make_video(project, d, **video_options)

    # 映像側はキー変更(song.key_shift)を参照しない。合成側がprojectを更新しても
    # 競合しないよう、変換直後のsnapshotを別スレッドへ渡す。
    visual_project = copy.deepcopy(project)
    planned_total = planned_video_total_sec(visual_project)
    abort = threading.Event()
    visual_failure: list[Exception] = []
    silent_video: Path | None = None
    silent_video_path = d / "video" / "video-only.mp4"
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video")
    runproc.set_cancel_check(lambda: job.cancel_event.is_set() or abort.is_set())

    def build_silent_video() -> Path:
        try:
            prepared = prepare_video(
                visual_project, d, planned_total, **video_options
            )
            return encode_silent_video(prepared)
        except Exception as exc:
            if not abort.is_set() and not job.cancel_event.is_set():
                visual_failure.append(exc)
            abort.set()
            runproc.kill_current()
            raise

    future = executor.submit(build_silent_video)
    try:
        try:
            _run_synthesize(job, config, project, synthesize)
            # 自動調整が決めたキー変更を保存し、伴奏も同じだけ移調する。
            project.save(d)
            with _stage(job, "mix"):
                audio_path = mix(project, d, soundfont=config.get("soundfont"))
        except Exception as audio_error:
            abort.set()
            runproc.kill_current()
            try:
                future.result()
            except Exception:
                pass
            if visual_failure:
                raise visual_failure[0] from audio_error
            raise

        with _stage(job, "video"):
            silent_video = future.result()
            actual_total = actual_video_total_sec(project, audio_path)
            frame_sec = 1.0 / int(video_options["fps"])
            if actual_total > planned_total + frame_sec:
                # 予定より長い音声をstream copyで延ばすことはできない。品質を
                # 優先し、この稀なケースだけ従来の1パス生成へ戻す。
                logger.warning(
                    "完成音声が動画予定尺を%.3f秒超えたため直列生成へ戻します",
                    actual_total - planned_total,
                )
                silent_video.unlink(missing_ok=True)
                silent_video = None
                return make_video(project, d, audio=str(audio_path), **video_options)
            return attach_audio(
                silent_video,
                audio_path,
                actual_total,
                out=d / "video" / "out.mp4",
            )
    finally:
        abort.set()
        runproc.kill_current()
        executor.shutdown(wait=True, cancel_futures=True)
        runproc.set_cancel_check(job.cancel_event.is_set)
        silent_video_path.unlink(missing_ok=True)


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
                client_hash=data.get("client_hash"),
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
        client_hash: str | None = None,
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
        job = Job(
            id=job_id,
            dir=job_dir,
            params=params,
            owner=owner,
            client_hash=client_hash,
        )
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

    def recent_client_count(self, client_hash: str, since: float) -> int:
        """Count persisted non-exempt jobs for one nonreversible IP identity."""
        return sum(
            1
            for job in self.jobs.values()
            if job.client_hash == client_hash and job.created_at >= since
        )

    def status_counts(self) -> dict[str, int]:
        """metrics向けの状態別件数。投入と同時でもdict反復を壊さない。"""
        counts = {name: 0 for name in ("queued", "running", "done", "error", "canceled")}
        with self._lock:
            for job in self.jobs.values():
                if job.status in counts:
                    counts[job.status] += 1
        return counts

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

        # 自作リストのeditorセッションも同じTTLで掃除する。保存処理は同じ
        # fingerprintを再利用するたびCSVを書き直すので、mtimeが最終利用時刻になる。
        from .editor_io import cleanup_editor_sessions

        cleanup_editor_sessions(
            self.config.get("editor_sessions"),
            hours * 3600,
            now=time.time() if now is None else now,
        )
        return [job.id for job in expired]

    def _save(self, job: Job) -> None:
        data = job.to_dict(with_log=False)
        # status.jsonは公開配信せずProtectHome/StateDirectory内に置く運用データなので、
        # 再起動後の診断用に実際の例外を保存する。API応答だけ上で一般化する。
        data["error"] = job.error
        # owner/finished_at はAPIのレスポンス(to_dict)には出さないが、再起動後も
        # 持ち主判定・自動削除ができるよう status.json には残す
        if job.owner:
            data["owner"] = job.owner
        if job.client_hash:
            data["client_hash"] = job.client_hash
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
    video_fps: int = 30,
    video_image_lead_sec: float = 0.1,
    parallel_video: bool = True,
) -> FastAPI:
    logging.getLogger("soramimic_video").setLevel(logging.INFO)
    from .editor_io import editor_sessions_dir

    jobs_dir.mkdir(parents=True, exist_ok=True)

    configured_ip_hash_key = os.environ.get(IP_HASH_KEY_ENV, "").strip()

    config: dict[str, Any] = {
        # 単語画像はジョブをまたいで共有する(初回ジョブの動画ステージが
        # 画像ダウンロードで数分かかるため。2回目以降はほぼゼロになる)
        "image_cache": jobs_dir.resolve() / "image-cache",
        # 生成前に出す仮サムネ(/api/thumbnail-preview)のPNGキャッシュ
        "preview_cache": preview_cache_dir(jobs_dir),
        # 自作リストで替え歌エディタを開いたときの単語リスト置き場(ジョブ横断)
        "editor_sessions": editor_sessions_dir(jobs_dir),
        "soundfont": resolve_soundfont(soundfont),
        "font": font or default_font(),
        "threads": threads,
        "layout": layout,
        "voicevox_url": voicevox_url,
        "video_fps": video_fps,
        "video_image_lead_sec": video_image_lead_sec,
        "parallel_video": parallel_video,
        # 合成の所要時間の目安(曲秒あたりの実処理秒)を実行ごとに記録して次回に使う
        "throughput_store": jobs_dir.resolve() / THROUGHPUT_FILENAME,
        # Missing configuration still enforces an in-process IP backstop, but
        # readiness reports it as non-persistent until operators provide a key.
        "ip_hash_key": (
            configured_ip_hash_key.encode("utf-8")
            if configured_ip_hash_key
            else secrets.token_bytes(32)
        ),
        "ip_hash_persistent": bool(configured_ip_hash_key),
    }
    manager = JobManager(jobs_dir, config)
    # 高コストGETの短期レート制限。セッション枠に加えてIP枠も必ず確認するため、
    # cookieを削除してもバックストップを回避できない。IP枠はNATを考慮して広め。
    get_session_limiter = RateLimiter(
        limit_env=GET_RATE_LIMIT_ENV,
        window_env=GET_RATE_WINDOW_ENV,
        default_limit=DEFAULT_GET_RATE_LIMIT,
        default_window=DEFAULT_GET_RATE_WINDOW,
    )
    get_ip_limiter = RateLimiter(
        limit_env=GET_IP_RATE_LIMIT_ENV,
        window_env=GET_IP_RATE_WINDOW_ENV,
        default_limit=DEFAULT_GET_IP_RATE_LIMIT,
        default_window=DEFAULT_GET_RATE_WINDOW,
    )
    get_hit_session_limiter = RateLimiter(
        limit_env=GET_CACHE_HIT_RATE_LIMIT_ENV,
        window_env=GET_RATE_WINDOW_ENV,
        default_limit=DEFAULT_GET_CACHE_HIT_RATE_LIMIT,
        default_window=DEFAULT_GET_RATE_WINDOW,
    )
    get_hit_ip_limiter = RateLimiter(
        limit_env=GET_CACHE_HIT_IP_RATE_LIMIT_ENV,
        window_env=GET_IP_RATE_WINDOW_ENV,
        default_limit=DEFAULT_GET_CACHE_HIT_IP_RATE_LIMIT,
        default_window=DEFAULT_GET_RATE_WINDOW,
    )
    preview_session_limiter = RateLimiter()
    get_slots = threading.BoundedSemaphore(GET_CONCURRENCY)
    # Final quota check and job persistence form one reservation across request threads.
    quota_submit_lock = threading.Lock()
    # よく使う単語リストの前処理(parse_tidy)は大きいリストだと数分かかる。
    # 指定があればバックグラウンドで先に構築しておき、初回変換も速くする
    start_warmup_thread()
    # Swagger/OpenAPIは専用の保護ルートとして後で登録する。FastAPI既定の無条件公開は切る。
    app = FastAPI(
        title="soramimic-video API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
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
        if is_simple_ui() and request.url.path in {
            "/api/editor-preview",
            "/api/editor-session",
            "/api/wordlist-check",
        }:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if is_simple_ui() and request.url.path.startswith("/editor/"):
            allowed_editor_path = request.url.path == "/editor/conf/setting.json" or bool(
                re.fullmatch(r"/editor/wordlists/[A-Za-z0-9_-]+\.csv", request.url.path)
            )
            if not allowed_editor_path:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        if is_simple_ui() and request.method == "POST" and request.url.path in {
            "/api/jobs",
            "/api/midi-check",
        }:
            maximum = int(
                _env_float(SIMPLE_MAX_REQUEST_BYTES_ENV, DEFAULT_SIMPLE_MAX_REQUEST_BYTES)
            )
            try:
                length = int(request.headers.get("content-length", ""))
            except ValueError:
                length = -1
            if maximum > 0 and (length < 0 or length > maximum):
                return JSONResponse({"detail": "入力が大きすぎます"}, status_code=413)
        if not is_public_mode() or request.url.path in {
            "/healthz",
            "/ogp-soramimic-v1.png",
            "/ogp-soramimic-v2.png",
            "/ogp-soramimic-v3.png",
            "/ogp-soramimic-v4.png",
            "/logo-soramimic-v1.png",
            "/logo-soramimic-symbol-v1.png",
            "/logo-soramimic-symbol-v2.png",
            "/logo-soramimic-symbol-v3.png",
            "/logo-soramimic-wordmark-v1.png",
            "/logo-soramimic-wordmark-v2.png",
            "/logo-soramimic-horizontal-v1.png",
            "/logo-soramimic-video-v1.png",
        }:
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

    def _peer_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(request.client.host) if request.client else None
        except ValueError:
            return None

    def _trusted_proxy(request: Request) -> bool:
        peer = _peer_ip(request)
        if peer is None:
            return False
        # loopbackも自動では信頼しない。cloudflared等のproxy CIDRを明示設定する。
        for raw in os.environ.get(TRUSTED_PROXY_IPS_ENV, "").split(","):
            try:
                if raw.strip() and peer in ipaddress.ip_network(raw.strip(), strict=False):
                    return True
            except ValueError:
                logger.warning("%s に不正なCIDRがあります", TRUSTED_PROXY_IPS_ENV)
        return False

    def _request_ip(request: Request) -> str:
        """信頼proxyだけCF接続元ヘッダを採用し、rate limitキーを偽装させない。"""
        if _trusted_proxy(request):
            forwarded = request.headers.get("cf-connecting-ip", "").strip()
            try:
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        peer = _peer_ip(request)
        return str(peer) if peer is not None else "unknown"

    def _client_hash(request: Request) -> str:
        """Return a short, nonreversible, deployment-keyed client identity."""
        return hmac.new(
            config["ip_hash_key"],
            _request_ip(request).encode("ascii", errors="strict"),
            hashlib.sha256,
        ).hexdigest()[:16]

    def _quota_exemption_allowlist() -> set[str]:
        return {
            canonical_email(raw)
            for raw in os.environ.get(QUOTA_EXEMPT_EMAILS_ENV, "").split(",")
            if canonical_email(raw)
        }

    def _access_issuer() -> str:
        issuer = os.environ.get(CF_ACCESS_TEAM_DOMAIN_ENV, "").strip()
        if issuer.endswith("/"):
            issuer = issuer[:-1]
        return issuer if valid_issuer(issuer) else ""

    def _access_config_valid() -> bool:
        return bool(
            _access_issuer()
            and os.environ.get(CF_ACCESS_AUD_ENV, "").strip()
        )

    async def _quota_exempt(request: Request) -> bool:
        """Grant quota exemption only to verified Access users behind local proxy."""
        allowlist = _quota_exemption_allowlist()
        peer = _peer_ip(request)
        if (
            not is_public_mode()
            or not allowlist
            or peer is None
            or not peer.is_loopback
            or not _trusted_proxy(request)
        ):
            return False
        assertion = request.headers.get("cf-access-jwt-assertion", "")
        issuer = _access_issuer()
        audience = os.environ.get(CF_ACCESS_AUD_ENV, "").strip()
        if not issuer or not audience:
            return False
        email = await run_in_threadpool(
            verify_access_email,
            assertion,
            issuer=issuer,
            audience=audience,
        )
        return email is not None and email in allowlist

    def _allow_expensive_get(
        request: Request,
        session_limiter: RateLimiter | None = None,
        *,
        cache_hit: bool = False,
    ) -> bool:
        if not is_public_mode():
            # private版に以前からあるthumbnail miss制限だけは互換維持する。
            if session_limiter is None or cache_hit:
                return True
            return session_limiter.allow(f"ip:{_request_ip(request)}")
        session = getattr(request.state, "session", None)
        session_key = f"session:{session}" if session else f"ip:{_request_ip(request)}"
        # 両方を評価する。短絡するとIP側の記録が抜け、cookieローテーションで回避できる。
        chosen_session = (
            get_hit_session_limiter if cache_hit else (session_limiter or get_session_limiter)
        )
        chosen_ip = get_hit_ip_limiter if cache_hit else get_ip_limiter
        session_ok = chosen_session.allow(session_key)
        ip_ok = chosen_ip.allow(f"ip:{_request_ip(request)}")
        return session_ok and ip_ok

    @contextmanager
    def _expensive_get_slot():
        if not is_public_mode():
            yield
            return
        if not get_slots.acquire(timeout=2.0):
            raise HTTPException(status_code=429, detail="画像処理が混み合っています")
        try:
            yield
        finally:
            get_slots.release()

    def _ops_allowed(request: Request) -> bool:
        if os.environ.get(EXPOSE_OPS_ENV, "").strip().lower() in ("1", "true", "yes"):
            return True
        # public版はloopbackでも自動許可しない(cloudflaredもloopbackに見えるため)。
        peer = _peer_ip(request)
        local_ops = not is_public_mode() or os.environ.get(
            ALLOW_LOCAL_OPS_ENV, ""
        ).strip().lower() in ("1", "true", "yes")
        if (
            local_ops
            and peer is not None
            and peer.is_loopback
            and not request.headers.get("cf-connecting-ip")
        ):
            return True
        expected = os.environ.get(OPS_TOKEN_ENV, "").strip()
        supplied = request.headers.get("x-soramimic-ops-token", "").strip()
        return bool(
            _trusted_proxy(request)
            and expected
            and supplied
            and secrets.compare_digest(expected, supplied)
        )

    def _require_ops(request: Request) -> None:
        if not _ops_allowed(request):
            # endpointの存在や認証方式を一般利用者に教えない。
            raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> JSONResponse:
        _require_ops(request)
        try:
            # XFの表記カナを発音形へ直す処理は、欠けても変換を止めずに
            # フォールバックする。そのままではデプロイ検証を通過しつつ選択語が
            # 変わるため、実際の解析器まで動かして必須依存を確認する。
            from .reading import particle_pronunciations

            particle_reading = particle_pronunciations("僕は花") == [(1, "ワ")]
        except (RuntimeError, ValueError):
            particle_reading = False
        checks: dict[str, bool] = {
            "jobs_dir_writable": jobs_dir.is_dir() and os.access(jobs_dir, os.W_OK),
            "persistent_ip_hash": (
                not is_public_mode() or bool(config["ip_hash_persistent"])
            ),
            "particle_reading": particle_reading,
        }
        if _quota_exemption_allowlist():
            checks["access"] = _access_config_valid()
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not ready", "checks": checks},
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> PlainTextResponse:
        _require_ops(request)
        counts = manager.status_counts()
        body = "".join(
            f'soramimic_jobs{{status="{status}"}} {count}\n'
            for status, count in counts.items()
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.get("/openapi.json", include_in_schema=False)
    def protected_openapi(request: Request) -> JSONResponse:
        _require_ops(request)
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    def protected_docs(request: Request) -> Response:
        _require_ops(request)
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")

    @app.get("/redoc", include_in_schema=False)
    def protected_redoc(request: Request) -> Response:
        _require_ops(request)
        from fastapi.openapi.docs import get_redoc_html

        return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")

    # editorの静的ビルド(scripts/build-editor.sh の出力)があれば /editor/ で配信する。
    # 無くてもサーバーは起動する(WebUIはeditor連携ボタンを隠すだけ)。
    editor_root = (editor_dist or DEFAULT_EDITOR_DIST).resolve()
    editor_available = (editor_root / "editor.html").is_file()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/ogp-soramimic-v1.png", include_in_schema=False)
    def ogp_image_v1() -> FileResponse:
        """旧URLを参照するSNSキャッシュ向けのimmutable OGP画像。"""
        return FileResponse(
            STATIC_DIR / "ogp-soramimic-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/ogp-soramimic-v2.png", include_in_schema=False)
    def ogp_image_v2() -> FileResponse:
        """SNSクローラ向けの版付きOGP画像。匿名sessionは発行しない。"""
        return FileResponse(
            STATIC_DIR / "ogp-soramimic-v2.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/ogp-soramimic-v3.png", include_in_schema=False)
    def ogp_image_v3() -> FileResponse:
        """デザイナー提供ロゴを使ったSNSクローラ向けOGP画像。"""
        return FileResponse(
            STATIC_DIR / "ogp-soramimic-v3.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/ogp-soramimic-v4.png", include_in_schema=False)
    def ogp_image_v4() -> FileResponse:
        """Canva提供ロゴを使ったSNSクローラ向けOGP画像。"""
        return FileResponse(
            STATIC_DIR / "ogp-soramimic-v4.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-v1.png", include_in_schema=False)
    def brand_logo_v1() -> FileResponse:
        """画面ヘッダー向けの版付きブランドロゴ。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-symbol-v1.png", include_in_schema=False)
    def brand_symbol_v1() -> FileResponse:
        """画面ヘッダー向けの版付き顔なしブランドシンボル。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-symbol-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-symbol-v2.png", include_in_schema=False)
    def brand_symbol_v2() -> FileResponse:
        """デザイナー正本から作成した版付き顔なしシンボル。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-symbol-v2.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-symbol-v3.png", include_in_schema=False)
    def brand_symbol_v3() -> FileResponse:
        """Canva提供の円周に沿ったシンボル。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-symbol-v3.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-wordmark-v1.png", include_in_schema=False)
    def brand_wordmark_v1() -> FileResponse:
        """デザイナー正本から作成した版付き文字ロゴ。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-wordmark-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-wordmark-v2.png", include_in_schema=False)
    def brand_wordmark_v2() -> FileResponse:
        """Canva提供の文字ロゴ。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-wordmark-v2.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-horizontal-v1.png", include_in_schema=False)
    def brand_horizontal_v1() -> FileResponse:
        """Canva提供のマーク・文字ロゴ横並び版。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-horizontal-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/logo-soramimic-video-v1.png", include_in_schema=False)
    def brand_video_v1() -> FileResponse:
        """Canva提供のSoramimic video完成ロゴ。"""
        return FileResponse(
            STATIC_DIR / "logo-soramimic-video-v1.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # 同梱サンプル曲(いずれも詞・曲パブリックドメイン、examples/gen_samples.py で生成)。
    # SORAMIMIC_SAMPLES_DIR を設定するとそのディレクトリのサンプルに差し替わる。
    def _sample_ids() -> set[str]:
        return {str(s["id"]) for s in visible_samples() if s.get("id")}

    @app.get("/api/samples")
    def list_samples() -> list[dict[str, Any]]:
        return visible_samples()

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
    async def get_config(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
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
            # 簡易UIでeditorボタンを隠しても、同梱のsetting.jsonから
            # 単語リスト選択肢は読むために別の能力値として返す。
            "wordlist_config": editor_available or is_simple_ui(),
            # 自作の単語リスト(CSV/zipアップロード)の受け入れ上限
            "max_wordlist_bytes": wordlist_csv_mod.max_bytes(),
            "max_wordlist_rows": wordlist_csv_mod.max_rows(),
            "max_wordlist_zip_bytes": wordlist_zip_mod.max_zip_bytes(),
            "max_wordlist_image_bytes": wordlist_zip_mod.max_image_bytes(),
            "max_wordlist_images": wordlist_zip_mod.max_images(),
        }
        if is_simple_ui():
            launch = load_launch_catalog()
            conf["simple_ui"] = True
            conf["launch_wordlists"] = launch.get("wordlists", [])
            conf["fixed_voicevox_style"] = int(launch.get("voicevox_style", 3003))
            # 初回版は「曲×単語リスト」の核だけを見せる。エディタと
            # 自作リストは後続アップデートで導線を開ける。
            conf["editor"] = False
        # 公開モードのときだけ、フロントに制限値とクレジット表示の要否を伝える
        if is_public_mode():
            conf["public"] = True
            quota_exempt = await _quota_exempt(request)
            conf["quota_exempt"] = quota_exempt
            if not quota_exempt:
                conf["daily_quota"] = int(
                    _env_float(DAILY_QUOTA_ENV, DEFAULT_DAILY_QUOTA)
                )
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

        wordlist = require_launch_wordlist(wordlist)
        try:
            with open(resolve_wordlist(wordlist), encoding="utf-8") as f:
                rows = csv.DictReader(f)
                first = next(rows, None)
                if first and first.get("image"):
                    return first
                return next((row for row in rows if row.get("image")), first)
        except (FileNotFoundError, OSError):
            return None

    @app.get("/api/wordlist-columns", dependencies=[Depends(_require_api_key)])
    def wordlist_columns(request: Request, wordlist: str = "") -> dict[str, Any]:
        """単語リストの列名一覧と代表行(レイアウト編集のWYSIWYG表示向け)。

        リストが未指定・見つからない場合も、替え歌単語のフィールドは返す。
        """
        from .convert import resolve_wordlist

        cols: list[str] = []
        row = None
        if wordlist.strip():
            wordlist = require_launch_wordlist(wordlist)
            if not _allow_expensive_get(request, cache_hit=True):
                raise HTTPException(status_code=429, detail="単語リストの取得が続いています")
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

    def _wordlist_has_image_url(wordlist: str, url: str) -> bool:
        """URLがimage列に実在するかを走査し、見つけ次第止める。"""
        from .convert import resolve_wordlist

        wordlist = require_launch_wordlist(wordlist)
        try:
            with open(resolve_wordlist(wordlist), encoding="utf-8") as f:
                return any(row.get("image") == url for row in csv.DictReader(f))
        except (FileNotFoundError, OSError):
            return False

    @app.get("/api/wordlist-image", dependencies=[Depends(_require_api_key)])
    def wordlist_image(request: Request, wordlist: str = "", url: str = "") -> FileResponse:
        """レイアウト編集プレビュー用の画像(WYSIWYG表示向け)。

        url指定時はプレビューのキュー画像を返す。オープンプロキシ化を避けるため、
        指定した単語リストのimage列に実在するURLだけを取得して返す。
        url未指定時は代表行(単語リストの最初の画像あり行)の画像。
        """
        from .video import cached_image, download_image

        wordlist = require_launch_wordlist(wordlist)
        # URL照合にもCSV走査が要るため、cache判定より先に広いhit枠を適用する。
        if not _allow_expensive_get(request, cache_hit=True):
            raise HTTPException(status_code=429, detail="画像の取得が続いています")

        if url:
            if not wordlist.strip() or not _wordlist_has_image_url(wordlist.strip(), url):
                raise HTTPException(status_code=404, detail="画像が見つかりません")
            target = url
        else:
            row = _sample_row(wordlist.strip()) if wordlist.strip() else None
            if not row or not row.get("image"):
                raise HTTPException(status_code=404, detail="画像のある行がありません")
            target = row["image"]
        cache_dir = jobs_dir.resolve() / "image-cache"
        path = cached_image(target, cache_dir)
        if path is None:
            if not _allow_expensive_get(request):
                raise HTTPException(status_code=429, detail="画像の取得が続いています")
            with _expensive_get_slot():
                # 待機中に別リクエストが保存していればネットワーク処理を繰り返さない。
                path = cached_image(target, cache_dir) or download_image(target, cache_dir)
        if path is None:
            raise HTTPException(status_code=404, detail="画像を取得できません")
        return FileResponse(path)

    def _sample_title(sample_id: str) -> tuple[str, str]:
        """サンプル曲の (曲名, 読み)。読みは samples.json の title_kana(無ければ空)。

        未知のIDは404。
        """
        if is_simple_ui() and sample_id not in launch_sample_ids():
            raise HTTPException(status_code=404, detail="そのサンプルはありません")
        entry = sample_entry(sample_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="そのサンプルはありません")
        return str(entry.get("title") or sample_id), str(entry.get("title_kana") or "")

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
        (そのときには作り直し済み=キャッシュヒットなので生成miss枠も変換も
        追加で消費しない)。

        ジョブではないので日次クォータは消費しないが、連打で変換が走り続けない
        ようキャッシュミス時だけセッション単位のレート制限をかける(超過は429)。
        UI側は429・エラー・タイムアウトのいずれでも単語リストの代表画像に
        フォールバックするので、ここで失敗してもモーダルの機能は壊れない。
        """
        from .convert import parse_convert_params
        from .thumbnail_preview import PreviewSpec, render_slot

        title, title_kana = _sample_title(sample)
        wordlist = require_launch_wordlist(wordlist)
        if not wordlist:
            raise HTTPException(status_code=400, detail="単語リスト名(wordlist)が必要です")
        # PreviewSpecの作成自体がCSV内容hash等を読むため、cache判定より前にも広い枠を置く。
        if not _allow_expensive_get(request, cache_hit=True):
            raise HTTPException(status_code=429, detail="プレビューの取得が続いています")
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
        if not _allow_expensive_get(request, preview_session_limiter):
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
        if is_simple_ui():
            raise HTTPException(status_code=404, detail="Not Found")
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
                sessions_dir=config["editor_sessions"],
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
        client_ip = _request_ip(request)
        if not verify_turnstile(token, client_ip):
            raise HTTPException(
                status_code=403,
                detail="人間かどうかの確認に失敗しました。"
                "ページを再読み込みしてもう一度お試しください。",
            )

    def _check_public_limits(
        owner: str | None,
        client_hash: str | None,
        midi_bytes: bytes,
        *,
        quota_exempt: bool = False,
    ) -> None:
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
        since = time.time() - 24 * 3600
        quota = int(_env_float(DAILY_QUOTA_ENV, DEFAULT_DAILY_QUOTA))
        if not quota_exempt and owner and quota > 0:
            used = manager.recent_count(owner, since)
            if used >= quota:
                raise HTTPException(
                    status_code=429,
                    detail=f"1日に作れる本数の上限({quota}本)に達しました。"
                    "24時間ほど空けてからまたお試しください。",
                )
        ip_quota = int(_env_float(IP_DAILY_QUOTA_ENV, DEFAULT_IP_DAILY_QUOTA))
        if not quota_exempt and client_hash and ip_quota > 0:
            used = manager.recent_client_count(client_hash, since)
            if used >= ip_quota:
                raise HTTPException(
                    status_code=429,
                    detail=f"この接続元から1日に作れる本数の上限({ip_quota}本)"
                    "に達しました。24時間ほど空けてからまたお試しください。",
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
        original_credit: str = Form(""),
        credit_notice: str = Form(""),
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
        quota_exempt = await _quota_exempt(request)
        midi_bytes = await read_midi_upload(midi)
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        launch_sample_id = require_launch_midi(midi.filename, midi_bytes)
        lyrics = require_launch_lyrics(launch_sample_id, lyrics)
        if is_simple_ui() and (
            (editor is not None and bool(editor.filename))
            or (wordlist_csv is not None and bool(wordlist_csv.filename))
            or bool(wordlist_text.strip())
            or any(bool(image.filename) for image in wordlist_images)
        ):
            raise HTTPException(
                status_code=422,
                detail="この入力形式は現在利用できません",
            )
        owner = owner_of(request)
        client_hash = None if quota_exempt or not is_public_mode() else _client_hash(request)
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
        if is_simple_ui():
            if editor_bytes is not None or custom is not None:
                raise HTTPException(
                    status_code=422,
                    detail="選択肢にある曲と単語リストを使ってください",
                )
            # 画面から選ばせないだけでなく、過去のlocalStorageや任意の
            # HTTPクライアントから来た値もここで固定する。
            launch = load_launch_catalog()
            synthesizer = "voicevox"
            voicevox_style = int(launch.get("voicevox_style", 3003))
            auto_octave = True
            transpose = 0
            model = "MERROW"
            layout = ""
            layout_json = ""
            original_credit = ""
            credit_notice = ""
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
        if is_simple_ui() and wordlist:
            wordlist = require_launch_wordlist(wordlist, status_code=422)
        # editor経由のジョブはJSON側の単語リスト指定がフォーム選択より優先される。
        # 履歴に実際の単語リスト名が残るよう、ここで解決して params に入れる
        if isinstance(editor_payload, dict):
            from .editor_io import (
                CUSTOM_WORDLIST_TEXT,
                _resolve_preview_wordlist,
                custom_wordlist_sid,
                is_original_wordlist,
                original_wordlist_text,
                session_wordlist_path,
            )

            sid = custom_wordlist_sid(editor_payload)
            if is_original_wordlist(editor_payload):
                # エディタの中で使った自作リスト。単語データ(csvText)がJSONに
                # 入っている自己完結の経路なので、サーバー側の置き場は見ない。
                # 中身は取り込み時にも検証するが、走らせてから落とさないよう
                # 受付時に型・大きさ・形をここで見る
                csv_text = original_wordlist_text(editor_payload)
                if csv_text is None:
                    raise HTTPException(
                        status_code=422,
                        detail="自作リストの単語データ(csvText)がありません。"
                        "替え歌エディタを開き直してから生成してください。",
                    )
                if len(csv_text.encode("utf-8")) > wordlist_csv_mod.max_bytes():
                    raise HTTPException(
                        status_code=413,
                        detail="自作リストが大きすぎます"
                        f"(上限は{wordlist_csv_mod.max_bytes() / 1024 / 1024:.1f}MBです)。",
                    )
                try:
                    wordlist_csv_mod.parse_editor_text(csv_text)
                except wordlist_csv_mod.WordlistCsvError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                # 履歴・ダウンロード名に出る表示名(リスト名では引けない)
                wordlist = CUSTOM_WORDLIST_TEXT
                where = ""  # 自作リストに絞り込み(ファセット)は無い
            elif sid:
                # 自作リストで作った替え歌。単語リスト行(=単語画像)は
                # editorセッションのCSVから引くので、無ければ受け付けない
                if session_wordlist_path(config["editor_sessions"], sid) is None:
                    raise HTTPException(
                        status_code=422,
                        detail="自作リストの単語データが見つかりません。"
                        "替え歌エディタを開き直してから生成してください。",
                    )
                # 履歴・ダウンロード名に出る表示名(リスト名では引けない)
                wordlist = CUSTOM_WORDLIST_TEXT
            else:
                resolved = _resolve_preview_wordlist(editor_payload, wordlist or None)
                if resolved:
                    wordlist = (
                        Path(resolved).stem if resolved.endswith(".csv") else resolved
                    )
        if is_simple_ui() and preview <= 0:
            launch_wordlists = {
                str(name) for name in load_launch_catalog().get("wordlists", [])
            }
            if wordlist not in launch_wordlists:
                raise HTTPException(
                    status_code=422,
                    detail="この単語リストは現在利用できません",
                )
            # レイアウトは単一の共通デザインではなく、選んだリストに
            # 対応する検証済みの既定デザインにサーバー側で固定する。
            layout = load_wordlist_layouts().get(wordlist, "")
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
            "original_credit": original_credit.strip(),
            "credit_notice": credit_notice.strip(),
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
        with quota_submit_lock:
            _check_public_limits(
                owner,
                client_hash,
                midi_bytes,
                quota_exempt=quota_exempt,
            )
            job = manager.create(
                midi_bytes, editor_bytes, lyrics, params,
                layout_json=layout_json, owner=owner, client_hash=client_hash,
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
        if is_simple_ui():
            raise HTTPException(status_code=404, detail="Not Found")
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
        require_launch_wordlist(name)
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

        midi_lines には XF歌詞の行テキストをそのまま並べて返す(align_lines が
        元歌詞との突き合わせに使うのと同じ表示テキスト)。自分のMIDIを選んだ
        人はこれを専用モーダルの元歌詞欄の下敷きに使う。
        """
        import tempfile

        from .align import align_lines
        from .xfparse import analyze_midi

        midi_bytes = await read_midi_upload(midi)
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
        launch_sample_id = require_launch_midi(midi.filename, midi_bytes)
        lyrics = require_launch_lyrics(launch_sample_id, lyrics)
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
                    "midi_lines": [],
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
            # 元歌詞欄の下敷き。表記が空の行は読みで代用する。
            "midi_lines": [ln.xf_surface or ln.xf_kana for ln in project.lines],
            "detail": "",
        }

    @app.post("/api/editor-session", dependencies=[Depends(_require_api_key)])
    async def editor_session(
        midi: UploadFile,
        lyrics: str = Form(""),
        wordlist: str = Form(""),
        where: str = Form(""),
        convert_params: str = Form(""),
        # セットアップ画面に出す曲名(表示用。UIの songTitleOf と同じもの)
        song_title: str = Form(""),
        # 変換までやるか、解析(MIDI→行ごとの読みカナ)だけで返すか。
        # 既定は従来どおり変換込み。convert=0 は「解析のみモード」で、
        # editor はセットアップ画面から始まってブラウザ内で変換する
        convert: bool = Form(True),
        # 自作の単語リスト。/api/jobs と同じ2通りの入口(zip/CSV1ファイル、または
        # 貼り付けテキスト+画像)。付いていればリスト名(wordlist)より優先する
        wordlist_csv: UploadFile | None = None,
        wordlist_text: str = Form(""),
        wordlist_images: list[UploadFile] = File(default_factory=list),
    ) -> dict[str, Any]:
        """MIDI(+単語リスト)から editor セッションJSONを組んで返す。

        WebUIがこれを sessionStorage["soramimic-editor"] に書いてから
        /editor/editor.html を iframe で開くと、そのまま編集を始められる。

        convert=1(既定)… run_pipeline の analyze→convert 段を同期・ジョブ無しで
        実行し、変換済み(results 入り)のJSONを返す。editor は編集画面から始まる。

        convert=0(解析のみ)… 変換はせず、セットアップ画面の材料
        (phrases/wordlist/param/where/song/noteLengthRawList/noteLengthAlpha)だけを返す。
        editor はセットアップ画面から始まり、「この設定で変換」でブラウザ内で
        変換してから編集画面に入る。単語リストは要らない(エディタで選べる)。

        自作リストのときは、editor がDBを組めるよう正規化済みCSVを
        editor-sessions/<sid>/ に置き、JSONの単語リスト設定を
        /editor/session-wordlists/<sid>.csv 向けにして返す。

        元歌詞(lyrics)は、どちらのモードでもシードの ``lyrics`` にそのまま
        載せて返す(ルビ記法も素通し)。editor はこれを元歌詞欄の初期値にし、
        書き出しJSONに ``lyrics``(編集後の生テキスト)を載せて返してくる——
        video が受け取るのはその生テキストだけで、字幕の行対応づけは従来どおり
        自前の align_lines で行う(:mod:`soramimic_video.editor_io` 参照)。
        """
        if is_simple_ui():
            raise HTTPException(status_code=404, detail="Not Found")
        import tempfile

        from .align import align_lines
        from .convert import (
            convert_project,
            parse_convert_params,
            pop_note_length_weight,
            project_note_length_weights,
            resolve_wordlist,
        )
        from .editor_io import (
            SESSION_WORDLIST_FILENAME,
            custom_wordlist_entry,
            export_editor,
            save_raw,
            seed_with_lyrics,
        )
        from .xfparse import analyze_midi

        midi_bytes = await midi.read()
        if not midi_bytes.startswith(b"MThd"):
            raise HTTPException(status_code=400, detail="MIDIファイルではありません")
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
        entry: dict[str, Any] | None = None
        conv_wordlist = ""
        conv_where: str | None = None
        if custom is not None:
            # 自作リストは名前で引けないので、editorが取りに来られる場所に置く
            sid = store_editor_session_wordlist(config["editor_sessions"], custom)
            conv_wordlist = str(
                config["editor_sessions"] / sid / SESSION_WORDLIST_FILENAME
            )
            # 絞り込み(where)の対象になる列を持たないので付けない(/api/jobs と同じ)
            entry = custom_wordlist_entry(sid)
        elif wordlist.strip():
            try:
                resolve_wordlist(wordlist.strip())
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            conv_wordlist = wordlist.strip()
            conv_where = where.strip() or None
        elif convert:
            raise HTTPException(
                status_code=422,
                detail="単語リスト名(wordlist)か自作リスト(wordlist_csv/wordlist_text)が必要です",
            )
        else:
            # 解析のみモードは単語リストが要らない(セットアップ画面で選ぶ)。
            # 初期選択のエントリを入れられないので、editor は conf の既定で始まる
            conv_where = where.strip() or None

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
            conv_params = parse_convert_params(convert_params)
            # Web UIのノート長設定はsoramimicへ移したが、変換済みセッションを
            # 直接作る経路も従来の画面既定0.25と同じ結果にする。
            conv_params.setdefault("NOTE_LENGTH_WEIGHT", "0.25")
            if not convert:
                return seed_with_lyrics(
                    editor_setup_seed(
                        project,
                        conv_wordlist,
                        conv_where,
                        conv_params,
                        wordlist_entry=entry,
                        song_title=song_title,
                    ),
                    lyrics,
                )
            try:
                raw = convert_project(
                    project,
                    wordlist=conv_wordlist,
                    where=conv_where,
                    params=conv_params,
                    # 自作リストはこの入力限りなので単語DBの共有キャッシュに載せない
                    cache_db=custom is None,
                )
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            save_raw(raw, d)
            project.save(d)
            # ホストはα=1の生重みを渡し、以後の設定と指数計算はsoramimicが担う。
            alpha = pop_note_length_weight(dict(conv_params))
            units = [line["units"] for line in raw["lines"]]
            raw_weights = project_note_length_weights(project, 1.0)(units)
            weights = (
                [
                    [value**alpha if value > 0 else 0.0 for value in row]
                    for row in raw_weights
                ]
                if raw_weights is not None and alpha > 0
                else None
            )
            path = export_editor(
                project,
                d,
                wordlist_entry=entry,
                weights_list=weights,
                note_length_raw_list=raw_weights,
                note_length_alpha=alpha,
            )
            return seed_with_lyrics(
                json.loads(path.read_text(encoding="utf-8")), lyrics
            )

    @app.get("/editor/session-wordlists/{sid}.csv")
    def editor_session_wordlist(sid: str) -> Response:
        """自作リストで開いたeditorのDB構築が取りに来るCSVを返す。

        editor JSONの wordlist.filepath = "session-wordlists/<sid>.csv" が
        /editor/session-wordlists/<sid>.csv に解決される。実体は
        /api/editor-session が置いた editor-sessions/<sid>/wordlist.csv。
        """
        from .editor_io import session_wordlist_path

        path = session_wordlist_path(config["editor_sessions"], sid)
        if path is None:
            raise HTTPException(status_code=404, detail="単語リストが見つかりません")
        text = path.read_text(encoding="utf-8")
        # editorのCSVパーサは行を素朴にsplitするので、末尾の改行があると
        # 最後に空行が混ざって落ちる。image列(サーバー上の絶対パス)も返さない
        return Response(
            content=_csv_without_image_column(text).rstrip("\n"),
            media_type="text/csv; charset=utf-8",
        )

    if editor_available and not is_simple_ui():
        # 上のルートで拾わなかった /editor/* は静的ビルドから配信する。
        # html=True で /editor/ と /editor/editor.html が引ける。
        app.mount(
            "/editor",
            StaticFiles(directory=editor_root, html=True),
            name="editor",
        )

    return app
