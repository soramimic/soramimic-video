"""動画生成ステージ: 単語画像+字幕(替え歌/元歌詞)+歌唱音源 → out.mp4。

構成:
 1. 単語リスト由来の画像をダウンロードし、レイアウト定義(layout.py)に従って
    列情報のテキストと合成した同一サイズのフレームPNGを作る
2. APIではconcatデマルチプレクサで無音H.264を歌声と並列生成し、最後に
   H.264をstream copyしてAAC音声だけを追加する。CLIのvideoコマンドは従来どおり
   ASS字幕と音声を1パスで合成する。どちらもH.264エンコードは1回だけ。

あわせて曲名を空耳変換したサムネ画像(thumbnail.py)をプロジェクトディレクトリに
作り、前奏区間(t=0〜歌い出し)のフレームとしても差し込む。

画像はWikimedia Commons等のURL(wordlist_rowのimage列)。クレジット表記が
必要な画像(CommonsでAttributionRequiredのもの)は出典文言をフレームに自動で
焼き込む(image_credit.py / layout.py参照。単語リストにimage_credit列があれば
その文言を優先)。あわせて image_page からクレジット一覧(credits.md)も生成
するので、公開時はライセンス表記に従うこと。

単語リストによっては画像がSVG(生成カード画像)なので、PillowがSVGを開けない
ぶんはダウンロード時にPNGへラスタライズしてキャッシュする(svg_to_png)。

フレームの左下には「lyrics & video by Soramimic」(歌声合成のクレジット表記が要るときは
「lyrics & video by Soramimic / VOICEVOX:キャラ名」)を小さく焼き込む。単語フレーム・
fallback・idle・サムネで共通で、レイアウトの "app_credit": false で外せる
(layout.py 参照)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter

import requests
from PIL import Image, ImageChops, ImageColor

from . import runproc
from .image_credit import USER_AGENT, fetch_image_credit, http_get_with_retry
from .kana import normalize_long_vowels
from .layout import (
    APP_CREDIT,
    DEFAULT_SUBTITLES,
    ImageElement,
    Layout,
    SubtitleElement,
    _font,
    _require_met,
    _SafeDict,
    is_missing,
    load_layout,
    render_frame,
    render_idle_frame,
    render_section_frame,
    resolve_font_path,
)
from .mix import MIX_DIR
from .project import ParodyWord, Project
from .synthesize import NEUTRINO_DIR
from .thumbnail import generate_thumbnail
from .xfparse import tick_to_sec

logger = logging.getLogger(__name__)

VIDEO_DIR = "video"
HOLD_MAX_SEC = 3.0  # 次の単語が来ないとき画像を表示し続ける最大時間
# 読めない短さでタイトルカードが点滅しないための最低表示時間。
THUMBNAIL_MIN_SEC = 1.0
SUB_PAD_SEC = 0.15  # 字幕を歌唱区間より少し早出し/遅消しする
# 「間奏(X秒)」を出す最短の間奏。これ未満は出しても一瞬で消えて目が滑るので出さない。
# 単語フレームは既定で最大 HOLD_MAX_SEC(3秒)残るため、間奏らしく見えるのはその後さらに
# 数秒空いたときで、5秒を下回る隙間は「歌の切れ目」であって間奏ではない
INTERLUDE_MIN_SEC = 5.0
# 後奏のエンドロールを出す最短の後奏。読み切れない長さでは出さない
OUTRO_MIN_SEC = 6.0
# 1枚に載る語数。既定のエンドロール(section_defaults.json)は最大8カラムで、
# 語が短ければ8列×15行=120語まで本文がフレーム高さの2%(1080pで約22px)を
# 切らずに入る。語が長いリストでは段数が自動で減り、そのぶん文字は小さくなる
ENDROLL_WORDS_PER_PAGE = 120
ENDROLL_MAX_PAGES = 4  # 分けるのは最大4枚まで(それ以上は1枚あたりの語数を増やす)
# 単語ページ1枚の目安表示時間。エンドロールは読み切るものではなく眺めるものなので、
# 後奏の長さで等分する(長い後奏だと1枚が延々と居座る)のはやめ、後奏の長さに
# よらず1枚およそこの秒数でめくる。実際のめくりは拍の切れ目にスナップするので
# 曲のテンポぶん前後し、余った時間は最後のクレジットページが吸収する
ENDROLL_PAGE_SEC = 3.0
# build_image_cues 前の画像/クレジットのプリフェッチ並列数。
# Commonsのサムネイル生成(Special:FilePath?width=)は並列4だと429が返ることを実測済み。
# 2なら429なしで、リトライ待ちが入る4より速かった(20枚: 24.6秒 vs 31.5秒)
IMAGE_FETCH_WORKERS = 2
# 最速サンプル(初音ミクの消失)には38.5msの音符がある。15fpsの66.7ms刻みでは
# 1モーラ分の表示が落ちうるため、時間解像度を保てる従来どおりの30fpsを既定にする。
DEFAULT_VIDEO_FPS = 30
# カードは発声と同時よりわずかに先に見せる方が、知覚上の遅れを感じにくい。
# 30fpsでは3フレーム。音声・字幕の時刻は動かさない。
DEFAULT_IMAGE_LEAD_SEC = 0.1
RENDERED_FRAME_CACHE_DIR = "rendered-frames"
RENDERED_FRAME_CACHE_TTL_SEC = 30 * 24 * 3600
RENDERED_FRAME_CACHE_MAX = 4000
IMAGE_CACHE_METADATA_DIR = ".metadata"
IMAGE_CACHE_REVALIDATE_SEC = 24 * 3600


def _run(cmd: list[str], what: str) -> None:
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{what}が失敗しました:\n{proc.stderr[-2000:]}")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("ffmpeg が見つかりません")
    return path


def _ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise RuntimeError("ffprobe が見つかりません")
    return path


def _audio_duration_sec(path: Path) -> float | None:
    """音声ファイルの実長(秒)をffprobeで取得する。取得できなければNone。

    ffprobeバイナリが無い/失敗する/出力をパースできない場合はいずれも
    警告ログを出してNoneを返す(動画生成自体は止めない)。
    """
    try:
        ffprobe_path = _ffprobe()
    except RuntimeError as exc:
        logger.warning("音声長の取得をスキップします: %s", exc)
        return None
    proc = runproc.run(
        [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        logger.warning("音声長の取得に失敗しました(%s): %s", path, proc.stderr[-500:])
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        logger.warning("音声長の取得結果を解析できませんでした(%s): %r", path, proc.stdout)
        return None


def _resolve_total_sec(sung_end_sec: float, audio_duration_sec: float | None) -> float:
    """動画の総尺(秒)を決める。

    後奏(エンディング)があると伴奏のMIDI長 = 音声の実長が、最後の歌唱ノート
    終端(+3秒の余韻)より長くなることがある。その場合は音声の実長に合わせて
    スライドショーを延ばし、後奏が映像側で切り詰められないようにする。
    音声長が取得できなかった場合は従来通り歌唱ノート側にフォールバックする。
    """
    if audio_duration_sec is None:
        return sung_end_sec
    return max(sung_end_sec, audio_duration_sec)


def extend_for_endroll(total_sec: float, sung_end_sec: float, words: list[str]) -> float:
    """後奏が足りない曲の総尺を、エンドロールが入るぶんだけ延ばす。

    後奏が短い(あるいは無い)曲でも「使った単語」一覧とクレジットは見せたいので、
    足りないときは動画の末尾に時間を足してエンドロール枠を作る(足した区間は
    音声が無いので、最終合成で無音をパディングする)。
    既に OUTRO_MIN_SEC 以上の後奏がある曲・使用単語が無い曲は何も変えない。
    """
    if not words or total_sec - sung_end_sec >= OUTRO_MIN_SEC:
        return total_sec
    pages = min(ENDROLL_MAX_PAGES, -(-len(words) // ENDROLL_WORDS_PER_PAGE))
    needed = (pages + 1) * ENDROLL_PAGE_SEC  # +1 は最後のクレジットページぶん
    return sung_end_sec + needed


# ---- 画像 ----


SVG_RASTER_WIDTH = 1280  # SVGをPNGに焼くときの幅(高さは元のviewBox比で決まる)


def looks_like_svg(data: bytes) -> bool:
    """バイト列がSVG(またはSVGを含むXML)かどうか。

    content-type では判定できない(GitHubのReleaseは .svg を
    application/octet-stream で返す)ので、データの先頭で見る。XML宣言や
    コメント・DOCTYPEが前置されることがあるので、先頭のいくらかに `<svg` が
    現れるかまで見る。
    """
    head = data[:1024].lstrip()
    if head.startswith(b"<svg"):
        return True
    return head.startswith((b"<?xml", b"<!--", b"<!DOCTYPE")) and b"<svg" in data[:4096]


def svg_to_png(data: bytes, width: int = SVG_RASTER_WIDTH) -> bytes | None:
    """SVGのバイト列をPNGに焼く(失敗したら警告してNone)。

    Pillowは自前でSVGを開けないので、フレーム合成の前にここでラスタライズする。
    幅だけ指定すれば cairosvg が元のviewBox比を保って高さを決める。
    cairosvg(とlibcairo)が入っていない環境ではNoneを返し、呼び出し側は
    「画像なし」として続行する(ジョブは落とさない)。
    """
    try:
        import cairosvg  # 重いので使うときだけ読む(api extra。要libcairo)
    except Exception as e:  # noqa: BLE001 - importエラーの種類は問わず画像なしに落とす
        logger.warning("SVGを変換できません(cairosvgが使えません): %s", e)
        return None
    try:
        return cairosvg.svg2png(bytestring=data, output_width=width)
    except Exception as e:  # noqa: BLE001 - 壊れたSVGでジョブを落とさない
        logger.warning("SVGをPNGに変換できませんでした: %s", e)
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    """同じURLを並列に落としても壊れたファイルを読ませないよう置換で書く。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _rasterized(path: Path) -> Path | None:
    """キャッシュ済みファイルがSVGならPNGに焼き直して差し替える(そうでなければそのまま)。

    以前のバージョンはSVGをそのまま(拡張子 .img で)キャッシュしていたので、
    既存キャッシュが残っている環境でも読み込み時にここでPNGへ移行する。
    変換できないときはNone(=画像なし)。SVGはキャッシュに残すので、
    毎回ダウンロードし直すことはなく、cairosvgを入れれば次から絵が出る。
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("キャッシュ画像を読めません: %s (%s)", path, e)
        return None
    if not looks_like_svg(data):
        return path
    png = svg_to_png(data)
    if png is None:
        return None
    out = path.with_suffix(".png")
    _write_atomic(out, png)
    if out != path:
        path.unlink(missing_ok=True)
    return out


# 黒背景(レイアウト既定)に置いたときに実質見えないとみなす最大輝度のしきい値。
# 透明背景の黒線画(SVG等)はアルファ合成しても黒のまま残るので、これ未満の画像は
# 文字フレーム(fallback)へ落とす(真っ黒の画像枠を作らない)。
INVISIBLE_IMAGE_MAX_LUMINANCE = 48


@lru_cache(maxsize=4096)
def image_is_visible(image_path: Path) -> bool:
    """黒背景へのアルファ合成後の最大輝度がしきい値以上あるか。

    読めない画像は見えないものとして扱う。キャッシュファイルは一度書いたら
    内容が変わらない(原子置換)ので、パスをそのままキャッシュキーにできる。
    """
    raw = _rasterized(image_path)
    if raw is None:
        return False
    try:
        with Image.open(raw) as source:
            im = source.convert("RGBA")
        _, _, _, a = im.split()
    except Exception:
        return False
    # 輝度は convert("L") の 0.299R+0.587G+0.114B。透明部分(RGBが残っていても
    # alpha=0)は黒(0)として合成してから最大値を取る
    composited = ImageChops.multiply(im.convert("L"), a)
    try:
        visible = composited.point(
            lambda value: 255 if value >= INVISIBLE_IMAGE_MAX_LUMINANCE else 0
        )
    except ValueError:  # pragma: no cover - 空画像は滅多にない
        return False
    return visible.getbbox() is not None


def _cached_raw(url: str, cache_dir: Path) -> Path | None:
    """キャッシュにあるファイル(SVGのままかもしれない)のパス。"""
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    for p in sorted(cache_dir.glob(f"{name}.*")):
        return p
    return None


def _image_metadata_path(url: str, cache_dir: Path) -> Path:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    return cache_dir / IMAGE_CACHE_METADATA_DIR / f"{name}.json"


def _read_image_metadata(url: str, cache_dir: Path) -> dict:
    path = _image_metadata_path(url, cache_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_image_metadata(url: str, cache_dir: Path, metadata: dict) -> None:
    path = _image_metadata_path(url, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(
        path,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode(),
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store_image_revision(
    url: str, cache_dir: Path, extension: str, data: bytes
) -> Path | None:
    """新しい画像内容を保存し、同じURLの旧拡張子ファイルだけを取り除く。"""
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    result = _store_image(cache_dir / f"{name}.{extension}", data)
    # SVG変換失敗時は _store_image が .svg を保存してNoneを返す。その新しいSVGを
    # revision本体として残し、同じURLの古いPNGを誤って選ばない。
    stored_svg = cache_dir / f"{name}.svg"
    kept = result or (stored_svg if stored_svg.exists() else _cached_raw(url, cache_dir))
    for old in cache_dir.glob(f"{name}.*"):
        if old != kept:
            old.unlink(missing_ok=True)
    image_is_visible.cache_clear()
    return result


def cached_image(url: str, cache_dir: Path) -> Path | None:
    """すでにキャッシュにある画像のパス(無ければ None)。ダウンロードは一切しない。

    ダウンロードを待てない用途(サムネのプレビュー生成など)向け。
    キーは download_image と同じ URL のsha1先頭16桁。
    キャッシュがSVGだったときだけ、その場でPNGへ焼き直して返す(通信はしない)。
    """
    from .asset_store import local_asset

    managed, asset = local_asset(url)
    if managed:
        return _rasterized(asset) if asset is not None else None
    raw = _cached_raw(url, cache_dir)
    return _rasterized(raw) if raw is not None else None


def download_image(
    url: str,
    cache_dir: Path,
    *,
    revalidate: bool = False,
    use_asset_store: bool = True,
    fetch_url_override: str | None = None,
) -> Path | None:
    """画像を取得し、同じURLは一定期間ごとにHTTP validatorsで更新確認する。

    キャッシュ済み画像は24時間そのまま利用する。期限後または revalidate=True では
    ETag / Last-Modifiedを使った条件付きGETを行い、304なら画像ファイルを書き換えない。
    validatorが無い配信元でも内容hashが同じなら書き換えないため、行や説明だけが
    変わった単語リストで画像キャッシュを削除する必要はない。
    """
    from .asset_store import local_asset

    if use_asset_store:
        managed, asset = local_asset(url)
        if managed:
            return _rasterized(asset) if asset is not None else None
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = _cached_raw(url, cache_dir)
    # ローカルパス / file:// はコピーで取り込む(生成・ローカル単語リストの画像用)
    local = url[7:] if url.startswith("file://") else url
    if "://" not in local:
        src = Path(local).expanduser()
        if not src.exists():
            logger.warning("画像が見つかりません: %s", url)
            return None
        if raw is not None:
            try:
                if src.stat().st_mtime_ns <= raw.stat().st_mtime_ns:
                    return _rasterized(raw)
            except OSError:
                pass
        ext = src.suffix.lstrip(".").lower() or "img"
        return _store_image_revision(url, cache_dir, ext, src.read_bytes())

    metadata = _read_image_metadata(url, cache_dir)
    checked_at = metadata.get("checked_at")
    cached_digest = metadata.get("cached_sha256")
    cache_consistent = False
    if raw is not None and isinstance(cached_digest, str) and cached_digest:
        try:
            cache_consistent = _file_sha256(raw) == cached_digest
        except OSError:
            pass
    if (
        raw is not None
        and cache_consistent
        and not revalidate
        and isinstance(checked_at, (int, float))
    ):
        if time.time() - checked_at < IMAGE_CACHE_REVALIDATE_SEC:
            return _rasterized(raw)

    fetch_url = fetch_url_override or url
    if fetch_url_override is None and "Special:FilePath" in url and "?" not in url:
        fetch_url = url + "?width=1200"  # フル解像度は不要なのでサムネイルをもらう
    headers = {"User-Agent": USER_AGENT}
    # 内容hashがメタデータと一致しない場合は、並行更新でvalidatorと画像が
    # 入れ違った可能性がある。条件付きGETの304を信用できないので無条件取得する。
    if raw is not None and cache_consistent:
        if metadata.get("etag"):
            headers["If-None-Match"] = str(metadata["etag"])
        if metadata.get("last_modified"):
            headers["If-Modified-Since"] = str(metadata["last_modified"])
    try:
        resp = http_get_with_retry(fetch_url, headers=headers, timeout=30)
    except requests.RequestException as e:
        logger.warning("画像の取得に失敗: %s (%s)", url, e)
        # 一時的な通信障害で既存動画から画像を消さない。失敗は記録せず次回再試行する。
        return _rasterized(raw) if raw is not None else None

    now = time.time()
    if resp.status_code == 304 and raw is not None:
        metadata["checked_at"] = now
        _write_image_metadata(url, cache_dir, metadata)
        return _rasterized(raw)

    digest = hashlib.sha256(resp.content).hexdigest()
    new_metadata = {
        "url": url,
        "checked_at": now,
        "etag": resp.headers.get("ETag", ""),
        "last_modified": resp.headers.get("Last-Modified", ""),
        "content_sha256": digest,
    }
    if (
        raw is not None
        and cache_consistent
        and metadata.get("content_sha256") == digest
    ):
        new_metadata["cached_sha256"] = _file_sha256(raw)
        _write_image_metadata(url, cache_dir, new_metadata)
        return _rasterized(raw)

    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp", "svg"):
        ext = "img"
    result = _store_image_revision(url, cache_dir, ext, resp.content)
    stored = _cached_raw(url, cache_dir)
    if stored is None:
        logger.warning("画像キャッシュへの保存に失敗: %s", url)
        return _rasterized(raw) if raw is not None else None
    new_metadata["cached_sha256"] = _file_sha256(stored)
    _write_image_metadata(url, cache_dir, new_metadata)
    return result


def _store_image(path: Path, data: bytes) -> Path | None:
    """取得したデータをキャッシュに置く。SVGはPNGに焼いてから置く。

    変換できなかったSVGは元のまま置いておく(次回もダウンロードし直さない)。
    """
    if not looks_like_svg(data):
        _write_atomic(path, data)
        return path
    png = svg_to_png(data)
    if png is None:
        _write_atomic(path.with_suffix(".svg"), data)
        return None
    out = path.with_suffix(".png")
    _write_atomic(out, png)
    return out


def image_cache_dir(work: Path, image_cache: Path | None = None) -> Path:
    """単語画像のダウンロード先。明示指定 > 環境変数 > 作業ディレクトリ内。

    ダウンロード画像はプロジェクトをまたいで使い回せる(URLベースのキー)ので、
    共有キャッシュを指定すると同じ単語リストの2回目以降が速くなる。
    """
    return image_cache or Path(
        os.environ.get("SORAMIMIC_VIDEO_IMAGE_CACHE") or work / "images"
    )


def prune_rendered_frame_cache(
    cache_dir: Path,
    ttl_sec: float = RENDERED_FRAME_CACHE_TTL_SEC,
    max_entries: int = RENDERED_FRAME_CACHE_MAX,
    now: float | None = None,
) -> list[Path]:
    """共有フレームPNGをTTL超過→上限超過の順で古いものから刈る。"""
    if not cache_dir.is_dir():
        return []
    current = time.time() if now is None else now
    entries: list[tuple[float, Path]] = []
    for path in cache_dir.glob("frame_*.png"):
        try:
            entries.append((path.stat().st_mtime, path))
        except OSError:
            continue
    removed = {path for mtime, path in entries if current - mtime > ttl_sec}
    kept = sorted(
        ((mtime, path) for mtime, path in entries if path not in removed),
        reverse=True,
    )
    removed.update(path for _, path in kept[max_entries:])
    for path in removed:
        path.unlink(missing_ok=True)
    if removed:
        logger.info("共有フレームキャッシュを%d件削除しました", len(removed))
    return sorted(removed)


def _black_frame(out_dir: Path, width: int, height: int) -> Path:
    out = out_dir / f"black_{width}x{height}.png"
    if not out.exists():
        # キュー画像が1枚も無いと out_dir(frames)は誰も作っていない。
        # ffmpegは親ディレクトリを作らず「Could not open file」で失敗する
        out_dir.mkdir(parents=True, exist_ok=True)
        _run(
            [_ffmpeg(), "-y", "-f", "lavfi", "-i", f"color=black:s={width}x{height}",
             "-frames:v", "1", "-update", "1", str(out)],
            "黒フレーム生成",
        )
    return out


def _prefetch_image_assets(frames: list[WordFrame], cache: Path) -> None:
    """逐次ループの前に、使う画像とクレジットをスレッドプールで温めておく。

    download_image はファイルキャッシュ(cache/<hash>.*)へ、fetch_image_credit は
    ファイルキャッシュ(cache/credits/<hash>.json)へ書き込むので、ここで先に走らせて
    おけば後続の逐次ループの同名呼び出しはネットワークに出ずキャッシュを読む。
    URLごとに1回だけ「画像→(必要なら)クレジット」の順で実行し、重複は排除する。

    キャンセル要求時は未実行のfutureをcancelして Cancelled を伝播する
    (実行中のダウンロードは高々 IMAGE_FETCH_WORKERS 本で、タイムアウトで終わる)。
    """
    # URLごとに: クレジット取得が要るか / どの image_page で引くか(初出を採用)
    need_credit: dict[str, str] = {}
    urls: list[str] = []
    for frame in frames:
        data = frame.data
        url = data.get("image") or ""
        if not url:
            continue
        if url not in need_credit and url not in urls:
            urls.append(url)
        if not str(data.get("image_credit") or "").strip() and url not in need_credit:
            need_credit[url] = data.get("image_page", "")
    if not urls:
        return

    def _fetch(url: str) -> None:
        raw = download_image(url, cache)
        # クレジットは画像が取れたものだけ(逐次ループの条件と揃える)
        if raw is not None and url in need_credit:
            fetch_image_credit(url, need_credit[url], cache)

    with ThreadPoolExecutor(max_workers=IMAGE_FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch, url): url for url in urls}
        try:
            for fut in as_completed(futures):
                runproc.raise_if_cancelled()  # プリフェッチ中でも中断できるように
                fut.result()  # Cancelled等の例外は握りつぶさず伝播(通常は例外なし)
        except BaseException:
            for f in futures:
                f.cancel()  # 未実行分は捨てる(実行中の分は__exit__が待つ)
            raise


# ---- タイムライン ----


@dataclass
class ImageCue:
    start: float
    end: float
    frame: Path


def word_frame_data(word: ParodyWord, row: dict) -> dict:
    """レイアウトのテンプレートに渡す1単語ぶんのデータ。

    単語リスト行の全列に、替え歌単語のフィールド(surface/kana/original等)を重ねる。
    build_image_cues とレイアウト編集プレビュー(editor JSON)で共用する。
    """
    return {
        **row,
        "surface": word.surface,
        "kana": word.kana,
        "original": word.original or row.get("original", ""),
        "original_surface": word.original_surface,
        "originalkana": word.originalkana,
    }


def idle_frame_data(project: Project, app_credit: str = "") -> dict:
    """idle(歌唱なし区間)フレームのテンプレートに渡すプロジェクトレベルの情報。

    単語データはないので、曲(入力MIDIのファイル名)と単語リスト名、それに
    全フレーム共通のアプリクレジット({app_credit})だけを渡す。
    """
    title = Path(project.song.midi_path).stem if project.song.midi_path else ""
    wordlist = project.parody.wordlist if project.parody else ""
    return {"title": title, "wordlist": wordlist, "app_credit": app_credit or APP_CREDIT}


# ---- 歌唱なし区間(前奏・間奏・後奏) ----


# エンドロールの単語一覧は1語1行。レイアウト側の columns で段組みに割り付ける
ENDROLL_WORD_SEP = "\n"


@dataclass
class IdleSection:
    """歌唱フレームが1枚も出ない区間と、その種別。

    kind は "intro"(1単語目より前=前奏) / "interlude"(単語と単語の間=間奏) /
    "outro"(最終単語より後=後奏)。レイアウトの同名キーで表示を出し分ける。
    """

    kind: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def idle_sections(cues: list[ImageCue], total_sec: float) -> list[IdleSection]:
    """キュー列の隙間を前奏・間奏・後奏に分類する。

    サムネのキューを載せた後(prepend_thumbnail_cue)に呼ぶ前提なので、前奏が
    サムネで埋まっている曲では intro は出てこない(=残りの隙間だけが対象)。
    表示できる単語が1つも無い曲は全編を前奏とみなす(間奏・後奏を定義できない)。
    """
    if total_sec <= 0:
        return []
    ordered = sorted(cues, key=lambda c: c.start)
    if not ordered:
        return [IdleSection("intro", 0.0, total_sec)]
    out: list[IdleSection] = []
    if ordered[0].start > 0:
        out.append(IdleSection("intro", 0.0, ordered[0].start))
    cursor = ordered[0].end
    for cue in ordered[1:]:
        if cue.start > cursor:
            out.append(IdleSection("interlude", cursor, cue.start))
        cursor = max(cursor, cue.end)
    if total_sec > cursor:
        out.append(IdleSection("outro", cursor, total_sec))
    return out


def sung_gap_sec(project: Project, section: IdleSection) -> float:
    """区間を挟む「歌が止まっている長さ」(直前の歌唱ノート終端〜次の歌唱ノート始端)。

    間奏フレームが出る区間は、直前の単語フレームの余韻(HOLD_MAX_SEC)のぶん
    実際の間奏より短い。「間奏(X秒)」の X には見ている人の感覚に合う
    「歌が止まっている長さ」を出したいので、ノートから測り直す。
    前後に歌唱ノートが無い(=間奏ではない)ときは区間そのものの長さを返す。
    """
    prev = [n.end_sec for n in project.notes if n.end_sec <= section.start]
    nxt = [n.start_sec for n in project.notes if n.start_sec >= section.end]
    if not prev or not nxt:
        return section.duration
    return max(section.duration, min(nxt) - max(prev))


def used_words(project: Project) -> list[str]:
    """使った替え歌単語の一覧(登場順・重複なし)。後奏のエンドロール用。

    単語リストの original 列(「新宿」「アインシュタイン」など元の語)を出す。
    リストに無い手入力の単語は original が空なので替え歌側の表記で代用する。
    filler(元歌詞のかなのまま残った区間)は単語リストの語ではないので数えない。
    """
    if project.parody is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in project.parody.lines:
        for w in line.words:
            if w.filler:
                continue
            label = (w.original or w.surface or "").strip()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    return out


def endroll_pages(
    words: list[str],
    duration: float = 0.0,
    per_page: int = ENDROLL_WORDS_PER_PAGE,
    max_pages: int = ENDROLL_MAX_PAGES,
) -> list[list[str]]:
    """使用単語をエンドロールのページに割り振る(1〜max_pages枚・語数は均等)。

    max_pages を超える語数は分割せず1枚あたりを増やす(文字は枠に合わせて縮む)。
    duration は「単語ページに使える時間」(後奏からクレジットページのぶんを引いた
    残り)で、1枚 ENDROLL_PAGE_SEC ずつ映すので、そこに収まらない枚数には割らない。
    詰めて読みにくいのと、めくりが速すぎて読めないのとでは後者の方が損なので、
    短い後奏では枚数を減らして1枚を詰める。
    """
    if not words:
        return []
    pages = min(max_pages, max(1, -(-len(words) // per_page)))
    if duration > 0:
        pages = min(pages, max(1, int(duration // ENDROLL_PAGE_SEC)))
    size = -(-len(words) // pages)
    return [words[i : i + size] for i in range(0, len(words), size)]


def beat_times(project: Project, until: float) -> list[float]:
    """曲の拍(4分音符)の時刻(秒)を0から until まで並べる。

    エンドロールのめくりを拍の切れ目に合わせるために使う。テンポ情報が無い
    プロジェクト(tempo_map が空・ticks_per_beat 不正)では空リストを返し、
    呼び出し側はスナップせず目安の秒数どおりにめくる。
    """
    tempo_map = project.song.tempo_map
    ticks_per_beat = project.song.ticks_per_beat
    if not tempo_map or ticks_per_beat <= 0 or until <= 0:
        return []
    out: list[float] = []
    tick = 0
    while True:
        sec = tick_to_sec(tick, tempo_map, ticks_per_beat)
        if sec > until:
            break
        # 異常なテンポ値(0など)で時刻が進まないときは無限ループにしない
        if out and sec <= out[-1]:
            break
        out.append(sec)
        tick += ticks_per_beat
    return out


def snap_to_beat(beats: list[float], target: float, lo: float, hi: float) -> float:
    """target に最も近い拍の時刻を返す(候補は lo〜hi の拍だけ)。

    範囲内に拍が無ければ(テンポ情報が無い・区間が短すぎる)target のまま返す。
    lo/hi を狭く取ることで、拍に合わせたぶんのずれが目安から離れすぎない。
    """
    candidates = [b for b in beats if lo < b < hi]
    if not candidates:
        return target
    return min(candidates, key=lambda b: abs(b - target))


def image_credits_text(credits: list[dict]) -> str:
    """使用画像のクレジットを1つの文言にまとめる(重複は順序を保って畳む)。

    動画本編では画像ごとに右下へ焼き込んでいる文言を、後奏でまとめて出すため。
    """
    texts = (str(c.get("credit") or "").strip() for c in credits)
    return " / ".join(dict.fromkeys(t for t in texts if t))


def section_frame_data(
    project: Project,
    app_credit: str = "",
    section: str = "idle",
    duration: float = 0.0,
    words: Sequence[str] = (),
    image_credits: str = "",
    page: int = 1,
    pages: int = 1,
    synth_credit: str = "",
    original_song: str = "",
    original_credit: str = "",
    credit_notice: str = "",
) -> dict:
    """区間フレームのテンプレートに渡す値(idle_frame_data に区間固有の列を足す)。

    - interlude_sec: その間奏の長さ(整数秒)。間奏以外では空文字
    - used_words: エンドロール用の使用単語(1語1行。段組みはレイアウト側の columns)
    - image_credits: 画像クレジットの集約。既定のエンドロールでは使っていない
      (各単語フレームの右下に個別のクレジットを焼いているため)が、そうした
      焼き込みをしないレイアウト向けに残してある
    - page / pages / page_label: エンドロールが複数枚に分かれたときのページ表示
      (1枚のときは page_label が空になり、見出しに「(1/1)」が出ない)
    - original_song: 元曲名
    - original_credit: 元曲の作詞・作曲・編曲等の著作者クレジット
    - credit_notice: 権利者やライセンスから指定された表記
    - synth_credit: 歌声合成側のクレジット表記(「VOICEVOX:四国めたん」など)。
      クレジットページで使う。表記が要らない合成では空文字なので、require で
      その行ごと出さない
    """
    data = idle_frame_data(project, app_credit)
    data.update(
        {
            "interlude_sec": str(int(round(duration))) if section == "interlude" else "",
            "used_words": ENDROLL_WORD_SEP.join(words),
            "image_credits": image_credits,
            "page": str(page),
            "pages": str(pages),
            "page_label": f"({page}/{pages})" if pages > 1 else "",
            "synth_credit": synth_credit,
            "original_song": original_song,
            "original_credit": original_credit,
            "credit_notice": credit_notice,
        }
    )
    return data


def build_section_cues(
    project: Project,
    cues: list[ImageCue],
    total_sec: float,
    layout: Layout,
    work: Path,
    width: int,
    height: int,
    app_credit: str = "",
    credits: list[dict] | None = None,
    synth_credit: str = "",
    original_song: str = "",
    original_credit: str = "",
    credit_notice: str = "",
) -> list[ImageCue]:
    """前奏・間奏・後奏の専用フレームをキューにする(専用定義が無い区間は空)。

    返したキューを既存のキューに混ぜて write_slideshow に渡すと、残った隙間だけが
    従来の idle(なければ黒)で埋まる。短い間奏(INTERLUDE_MIN_SEC 未満)や短い
    後奏(OUTRO_MIN_SEC 未満)には出さない。

    後奏は「使った単語」を1枚 ENDROLL_PAGE_SEC ずつ拍の切れ目でめくり、最後に
    クレジットページ("credits" 区間)で残りを埋める。
    """
    out: list[ImageCue] = []
    frames_dir = work / "frames"
    words = used_words(project)
    credit_text = image_credits_text(credits or [])
    for sec in idle_sections(cues, total_sec):
        if not layout.has_section(sec.kind):
            continue
        if sec.kind == "interlude" and sec.duration < INTERLUDE_MIN_SEC:
            continue
        if sec.kind == "outro":
            # 後奏が短い曲・使用単語が取れない曲ではエンドロールを出さない
            if sec.duration < OUTRO_MIN_SEC or not words:
                continue
            show_credits = layout.has_section("credits")
            # クレジットページに最低1枚ぶんを残し、残りを単語ページに割り振る
            word_sec = sec.duration - (ENDROLL_PAGE_SEC if show_credits else 0.0)
            pages = endroll_pages(words, word_sec)
            beats = beat_times(project, sec.end)
            t = sec.start
            for i, page_words in enumerate(pages):
                data = section_frame_data(
                    project, app_credit, "outro", sec.duration,
                    page_words, credit_text, i + 1, len(pages),
                    synth_credit=synth_credit,
                    original_song=original_song,
                    original_credit=original_credit,
                    credit_notice=credit_notice,
                )
                frame = render_section_frame(
                    layout, data, width, height, frames_dir, "outro"
                )
                # 目安は ENDROLL_PAGE_SEC 後。その前後半分の範囲に拍があれば
                # そこでめくる(伴奏の切れ目と揃うと機械的に見えない)
                end = min(
                    sec.end,
                    snap_to_beat(
                        beats,
                        t + ENDROLL_PAGE_SEC,
                        t + ENDROLL_PAGE_SEC / 2,
                        min(sec.end, t + ENDROLL_PAGE_SEC * 1.5),
                    ),
                )
                if i == len(pages) - 1 and not show_credits:
                    # クレジットを出さないレイアウトでは最後の1枚で後奏を覆う
                    # (黒画面の尻尾を作らない)
                    end = sec.end
                if frame is not None:
                    out.append(ImageCue(start=t, end=end, frame=frame))
                t = end
            if show_credits and t < sec.end:
                data = section_frame_data(
                    project, app_credit, "credits", sec.duration,
                    image_credits=credit_text, synth_credit=synth_credit,
                    original_song=original_song,
                    original_credit=original_credit,
                    credit_notice=credit_notice,
                )
                frame = render_section_frame(
                    layout, data, width, height, frames_dir, "credits"
                )
                if frame is not None:
                    # 単語ページの余りを全部吸収して後奏の終わりまで出す
                    out.append(ImageCue(start=t, end=sec.end, frame=frame))
            continue
        # 間奏の「X秒」は区間の長さではなく歌が止まっている長さ(直前の余韻を含む)
        shown = sung_gap_sec(project, sec) if sec.kind == "interlude" else sec.duration
        data = section_frame_data(project, app_credit, sec.kind, shown)
        frame = render_section_frame(layout, data, width, height, frames_dir, sec.kind)
        if frame is not None:
            out.append(ImageCue(start=sec.start, end=sec.end, frame=frame))
    return out


def app_credit_text(
    synth_credit: str = "",
    original_credit: str = "",
    credit_notice: str = "",
) -> str:
    """フレームに焼き込むクレジット文言。

    既定は「lyrics & video by Soramimic」。歌声合成側にもクレジット表記が要るとき
    (VOICEVOXのキャラ名など)や、元曲・権利者の表記があるときは後ろに足す。
    元曲情報はエンドロールにも詳しく出すが、必要な表記が動画から切り離されないよう
    全フレームの署名にも焼き込む。
    """
    synth = (synth_credit or "").strip()
    original = (original_credit or "").strip()
    notice = (credit_notice or "").strip()
    parts = [APP_CREDIT]
    if synth:
        parts.append(synth)
    if original:
        parts.append(f"Original: {original}")
    if notice:
        parts.append(notice)
    return " / ".join(parts)


def word_is_shown(layout: Layout, data: dict, use_fallback: bool) -> bool:
    """このレイアウトでこの単語に表示できるもの(画像 or テキスト)があるか。

    build_image_cues が「表示するものがない単語」をキューから外す判定と同じ。
    """
    return bool(data.get("image")) or any(layout.render_texts(data, use_fallback))


def effective_fallback(
    layout: Layout, data: dict, use_fallback: bool, has_image: bool
) -> bool:
    """画像が無い単語を、未知語と同じfallbackへ落とす判定。

    既知語でも画像列が空だったり画像が取得できなかったときに、何も出さない
    のではなく文字フレーム(fallback)で描くために使う。画像枠をレイアウトに持つ
    単語で画像が無いと、その枠が真っ黒のまま残るのでfallbackへ落とす。
    editorのキュープレビュー(editor_io)と build_image_cues で共用する。
    """
    if use_fallback or has_image:
        return use_fallback
    values = _SafeDict(
        # NA等の欠損マーカーは「NA年生まれ」と描画されてしまうので空文字に潰す
        {k: ("" if is_missing(v) else v) for k, v in data.items() if v is not None}
    )
    # 通常側で実際に描かれる画像要素がある(=画像枠が黒抜けになる)レイアウトは
    # テキストが残っていてもfallbackへ落とす(例: gimukyoiku_card の左半分)。
    if any(
        isinstance(el, ImageElement) and _require_met(el, values)
        for el in layout.active_elements(False)
    ):
        return True
    return not any(layout.render_texts(data, False))


@dataclass
class WordFrame:
    """1単語ぶんのフレーム候補(表示区間+テンプレートに渡すデータ)。"""

    line_id: int
    start: float
    end: float
    data: dict
    use_fallback: bool


# レイアウトのテンプレートに出てくるが単語リストの列ではない変数
# (word_frame_data / build_image_cues がレイアウトへ渡すフィールド)。
# 列名の食い違い検知(layout_column_mismatch)で除外する。
NON_COLUMN_FIELDS = frozenset(
    {
        "surface",
        "kana",
        "original",
        "original_surface",
        "originalkana",
        "image",
        "image_credit",
        "app_credit",
    }
)


def layout_template_columns(layout: Layout) -> set[str]:
    """レイアウトの通常側要素が参照する単語リスト列名({prefecture} など)。

    text要素のテンプレート変数と、要素の出し分け条件(require/require_empty)の
    列名を集める。単語フィールド(NON_COLUMN_FIELDS)は列ではないので除く。
    """
    columns: set[str] = set()
    for el in layout.elements:
        template = getattr(el, "template", "")
        if template:
            for _, field_name, _, _ in Formatter().parse(template):
                if field_name:
                    # {a[b]} や {a.b} も先頭の名前だけ見る
                    columns.add(re.split(r"[.\[]", field_name)[0])
        for attr in ("require", "require_empty"):
            name = getattr(el, attr, None)
            if name:
                columns.add(str(name))
    return columns - NON_COLUMN_FIELDS


def layout_column_mismatch(layout: Layout, row_keys: set[str]) -> list[str]:
    """レイアウトが参照する列が単語リストに1つも無いときに、その列名を返す。

    別の単語リスト向けのレイアウト(例: scientist のジョブに station_card)が
    当たると、列参照がすべて空になり「名前と写真だけ」のカードになる。
    列が1つでも一致していれば単なる任意列の欠落なので空リストを返す。
    """
    columns = layout_template_columns(layout)
    if not columns or not row_keys or columns & row_keys:
        return []
    return sorted(columns)


def collect_word_frames(project: Project, layout: Layout) -> list[WordFrame]:
    """このレイアウトで表示できる替え歌単語を、歌唱順に並べたフレーム候補列。

    画像のダウンロード結果に依存しない純粋な計算で、build_image_cues(実際の
    フレーム生成)と build_ass(字幕の消灯タイミング)が同じ並び・同じ時間軸を
    共有するために使う。
    """
    if project.parody is None:
        return []
    frames: list[WordFrame] = []
    row_keys: set[str] = set()
    for pline in project.parody.lines:
        for w in pline.words:
            row = w.wordlist_row or {}
            row_keys |= set(row)
            # 単語リストに行がない単語(手入力の未知語など)はfallback側で描く
            use_fallback = not row
            # レイアウトのテンプレートには行の全列+替え歌単語のフィールドを渡す
            data = word_frame_data(w, row)
            # 画像列が空の既知語も文字フレーム(fallback)で出す
            use_fallback = effective_fallback(
                layout, data, use_fallback, has_image=bool(data.get("image"))
            )
            if not word_is_shown(layout, data, use_fallback):
                continue  # このレイアウトでは表示できるものがない単語
            start, end = project.word_time_range(w)
            frames.append(WordFrame(pline.line_id, start, end, data, use_fallback))
    frames.sort(key=lambda f: f.start)
    missing = layout_column_mismatch(layout, row_keys)
    if missing:
        logger.warning(
            "レイアウトが参照する列が単語リストにありません: %s "
            "(単語リスト=%s の列=%s)。別の単語リスト向けのレイアウトが"
            "当たっている可能性があります",
            ", ".join(missing),
            project.parody.wordlist,
            ", ".join(sorted(row_keys)),
        )
    return frames


def frame_show_end(frames: list[WordFrame], i: int, hold_next: bool) -> float:
    """frames[i] の表示を次の単語まで持続させたときの終了時刻。

    既定は最大 HOLD_MAX_SEC 秒。hold_next(レイアウトの "hold": "next")なら
    次の単語まで隙間を埋め続ける(間奏まるごと持続する)。最終単語より後(後奏)は
    hold_next でも持続させず idle/黒に任せる。
    字幕(build_ass)も行末の単語のこの値に合わせて消える。
    """
    end = frames[i].end
    if i + 1 >= len(frames):
        return end if hold_next else end + HOLD_MAX_SEC
    next_start = frames[i + 1].start
    if hold_next:
        return max(end, next_start)
    return min(max(end, next_start), end + HOLD_MAX_SEC)


def line_show_ends(project: Project, layout: Layout) -> dict[int, float]:
    """行ID -> その行の最後の単語フレームの表示終了時刻(単語フレームが無い行は含まない)。"""
    frames = collect_word_frames(project, layout)
    return {
        f.line_id: frame_show_end(frames, i, layout.hold_next) for i, f in enumerate(frames)
    }


def build_image_cues(
    project: Project,
    work: Path,
    width: int,
    height: int,
    image_cache: Path | None = None,
    layout: Layout | None = None,
    app_credit: str = "",
    image_lead_sec: float = DEFAULT_IMAGE_LEAD_SEC,
) -> tuple[list[ImageCue], list[dict]]:
    """替え歌単語の歌唱区間に対応するフレームキュー列と、使用画像のクレジット情報。

    フレームは単語リスト行の画像+列情報をレイアウト定義で合成したもの。
    画像がなくてもレイアウトのtext要素が埋まる単語はテキストのみで表示する。
    app_credit は全フレームの隅に焼き込む署名(既定は「lyrics & video by Soramimic」)。
    image_lead_sec はカードだけを歌唱より先に出す秒数。字幕・音声は変更しない。
    """
    if image_lead_sec < 0:
        raise ValueError("image_lead_sec は0以上で指定してください")
    if project.parody is None:
        return [], []
    if layout is None:
        layout = load_layout(None)
    frames = collect_word_frames(project, layout)

    cues: list[ImageCue] = []
    credits: dict[str, dict] = {}
    cache = image_cache_dir(work, image_cache)
    # 画像と同じ共有キャッシュ配下へ置き、同じ単語・レイアウトのPNGをジョブ間で再利用する
    norm = cache / RENDERED_FRAME_CACHE_DIR
    # 描画前に刈る。描画後だと、上限を超える巨大ジョブでこのあとffmpegが読む
    # フレームまで削除しかねない。
    prune_rendered_frame_cache(norm)
    # 逐次ループが読む画像/クレジットを先に並列で温める(キャッシュが冷えていると
    # 1単語あたり画像DL+クレジット取得で数秒かかり、単語数ぶん直列に積み上がるため)
    _prefetch_image_assets(frames, cache)
    for i, wf in enumerate(frames):
        # 画像・見出し・説明を含むカード全体だけを少し先行表示する。
        # 音声と字幕はprojectの元時刻を使い続ける。
        start = max(0.0, wf.start - image_lead_sec)
        data, use_fallback = wf.data, wf.use_fallback
        # 全フレーム共通の署名(レイアウトが左下に焼き込む)
        data["app_credit"] = app_credit or APP_CREDIT
        runproc.raise_if_cancelled()  # 画像ダウンロード中でも中断できるように
        url = data.get("image") or ""
        raw = download_image(url, cache) if url else None
        if raw is not None and not image_is_visible(raw):
            # 黒背景カード上で実質見えない画像(黒線+透明SVGなど)は無いものとして
            # 扱い、文字フレーム(fallback)へ落とす(真っ黒の画像枠を残さない)
            logger.info("画像が黒背景上で見えないため文字フレームにします: %s", url)
            raw = None
        if raw is None:
            # 画像が取得できなかった既知語も未知語と同じ文字フレームに落とす
            use_fallback = effective_fallback(layout, data, use_fallback, has_image=False)
            if not any(layout.render_texts(data, use_fallback)):
                continue  # 画像が取れずテキストもないフレームは出さない
        # 画像クレジット文言: 単語リストのimage_credit列があればそれを、なければ
        # Commonsから取得(表記不要な画像では空になり、フレームには描かれない)
        if raw is not None and url and not str(data.get("image_credit") or "").strip():
            info = fetch_image_credit(url, data.get("image_page", ""), cache)
            if info is not None:
                data["image_credit"] = info["credit_text"]
        frame = render_frame(layout, raw, data, width, height, norm, use_fallback)
        if frame is None:
            continue
        # 次の単語までの持続時間もカード全体と同じ量だけ前へ動かす。
        # 字幕はprojectの元時刻を使うため、この先行量の影響を受けない。
        show_end = max(
            start,
            frame_show_end(frames, i, layout.hold_next) - image_lead_sec,
        )
        if cues and cues[-1].end > start:
            cues[-1].end = start
        cues.append(ImageCue(start=start, end=show_end, frame=frame))
        if url and raw is not None and url not in credits:
            credits[url] = {
                "word": data["surface"],
                "original": data["original"],
                "image": url,
                "image_page": data.get("image_page", ""),
                "credit": str(data.get("image_credit") or ""),
            }
    return cues, list(credits.values())


def thumbnail_show_end(project: Project) -> float:
    """サムネを出す区間の終わり(=前奏の終わり)。出さないときは0を返す。

    字幕(ASS)は歌唱区間の SUB_PAD_SEC 秒前から出るので、そこで打ち切って
    サムネと字幕が重ならないようにする。前奏が短くてサムネが一瞬しか
    出せない曲(THUMBNAIL_MIN_SEC 未満)では、点滅させるより出さない方が
    見やすいので0を返す(サムネ画像自体はSNS投稿用に作る)。
    """
    starts = [n.start_sec for n in project.notes if n.kana] or [
        n.start_sec for n in project.notes
    ]
    end = min(starts, default=0.0) - SUB_PAD_SEC
    return end if end >= THUMBNAIL_MIN_SEC else 0.0


def prepend_thumbnail_cue(
    cues: list[ImageCue], frame: Path, end: float
) -> list[ImageCue]:
    """先頭に [0, end) のサムネキューを足し、被る単語キューを後ろへ詰める。

    スライドショーはキューを順に連結するので(write_slideshow)、区間が重なると
    以降の映像がまるごと後ろへずれる。サムネに完全に隠れるキューは捨て、
    途中から出るキューは開始をサムネの終わりに合わせる。
    """
    if end <= 0:
        return cues
    kept: list[ImageCue] = []
    for cue in cues:
        if cue.end <= end:
            continue  # サムネに完全に覆われる単語(字幕はそのまま焼かれる)
        kept.append(
            ImageCue(start=max(cue.start, end), end=cue.end, frame=cue.frame)
        )
    return [ImageCue(start=0.0, end=end, frame=frame), *kept]


def _write_slideshow_concat(
    cues: list[ImageCue],
    work: Path,
    width: int,
    height: int,
    total_sec: float,
    idle_frame: Path | None = None,
) -> Path:
    """静止画タイムラインをffconcatファイルへ書き出す。

    歌唱がない隙間(前奏・間奏・後奏)は idle_frame があればそれで、なければ黒で埋める。
    """
    fill = idle_frame or _black_frame(work / "frames", width, height)
    entries: list[tuple[Path, float]] = []
    cursor = 0.0
    for cue in cues:
        if cue.start > cursor:
            entries.append((fill, cue.start - cursor))
        entries.append((cue.frame, cue.end - cue.start))
        cursor = cue.end
    if total_sec > cursor:
        entries.append((fill, total_sec - cursor))

    lines = ["ffconcat version 1.0"]
    for path, dur in entries:
        if dur <= 0:
            continue
        lines.append(f"file '{path.resolve()}'")
        lines.append(f"duration {dur:.3f}")
    # concatの仕様: 最後のファイルは duration が無視されることがあるため再掲する
    if entries:
        lines.append(f"file '{entries[-1][0].resolve()}'")
    concat_path = work / "slideshow.txt"
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return concat_path


def write_slideshow(
    cues: list[ImageCue],
    work: Path,
    width: int,
    height: int,
    total_sec: float,
    idle_frame: Path | None = None,
) -> Path:
    """スライドショーだけを動画化する(互換用ヘルパー)。

    本番の make_video は中間動画を作らず、ffconcatを字幕・音声と一緒に
    直接エンコードする。この関数はスライドショー単体が必要な呼び出しとテスト用に残す。
    """
    concat_path = _write_slideshow_concat(
        cues, work, width, height, total_sec, idle_frame
    )

    out = work / "slideshow.mp4"
    _run(
        [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
         "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "fast", str(out)],
        "スライドショー生成",
    )
    return out


# ---- 字幕 ----


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_color(color: str) -> str:
    """CSS風の色(名前 / #rrggbb / #rrggbbaa)をASSの &HAABBGGRR に変換する。"""
    rgba = ImageColor.getrgb(color)
    r, g, b = rgba[:3]
    a = rgba[3] if len(rgba) == 4 else 255
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def _ass_alignment(el: SubtitleElement) -> int:
    """align/valign をASSのnumpad Alignment値にする。"""
    base = {"bottom": 1, "middle": 4, "top": 7}.get(el.valign, 1)
    return base + {"left": 0, "center": 1, "right": 2}.get(el.align, 1)


WORD_SEP = "  "  # 替え歌字幕で単語を区切る空白(build_ass本文とルビ位置計算で共有)

# ひらがな→カタカナ(表記が既にカナかを判定するための正規化用)
_HIRA_TO_KATA = {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}


def _to_katakana(text: str) -> str:
    return "".join(_HIRA_TO_KATA.get(ch, ch) for ch in text)


_KATA_TO_HIRA = {v: k for k, v in _HIRA_TO_KATA.items()}


def _to_hiragana(text: str) -> str:
    """ルビ表示用にカタカナをひらがなへ(長音「ー」などはそのまま)。"""
    return "".join(_KATA_TO_HIRA.get(ch, ch) for ch in text)


def _is_all_kana(text: str) -> bool:
    return all(ch in _KANA_CHARS for ch in text)


# 読みを持たない記号(区切り・つなぎ)。表記には出るが読みには現れないので、
# ルビの要否判定でも読みの割り付けでも「無いもの」として扱う。
# (例: 「バリッシュ・コノル」の読みは「バリッシュコノル」で中黒は現れない)
# 中黒(全角・半角)、イコール類(人名の区切り)、空白。
_SILENT_SYMBOLS = frozenset("・･＝=゠ 　")


def _strip_silent(text: str) -> str:
    """読みを持たない記号を取り除く。"""
    return "".join(ch for ch in text if ch not in _SILENT_SYMBOLS)


def _needs_ruby(surface: str, kana: str) -> bool:
    """この単語にルビを振るべきか。

    表記が全部カナなら読みは表記から自明なのでルビ不要(カタカナ表記に
    ひらがなルビを重ねない)。カナ以外(漢字等)を含む場合は、ひらがな/
    カタカナ・長音表記のゆれを吸収したうえで読みと違うときだけ振る。
    読みを持たない記号(中黒等)は判定前に除去する(「バリッシュ・コノル」は
    実質全カナなのでルビ不要)。
    """
    if not surface or not kana:
        return False
    bare = _strip_silent(surface)
    if _is_all_kana(bare):
        return False
    a = normalize_long_vowels(_to_katakana(bare))
    b = normalize_long_vowels(_to_katakana(kana))
    return a != b


# そのまま読める文字(ひらがな・カタカナ・長音記号など)。部分ルビのラン分割用
_KANA_CHARS = frozenset(
    [chr(c) for c in range(0x3041, 0x3097)]  # ひらがな
    + [chr(c) for c in range(0x30A1, 0x30FB)]  # カタカナ(ァ〜ヺ)
    + list("ーゝゞヽヾ")
)


def _kana_runs(surface: str) -> list[tuple[int, int, bool]]:
    """表記をカナ/非カナの連続ランに分ける。(開始idx, 終了idx, カナか) の列。

    読みを持たない記号(中黒等)はカナ側に含める(ルビを振る対象ではないため)。
    """
    runs: list[tuple[int, int, bool]] = []
    for i, ch in enumerate(surface):
        is_kana = ch in _KANA_CHARS or ch in _SILENT_SYMBOLS
        if runs and runs[-1][2] == is_kana:
            runs[-1] = (runs[-1][0], i + 1, is_kana)
        else:
            runs.append((i, i + 1, is_kana))
    return runs


def _ruby_segments(surface: str, kana: str) -> list[tuple[int, int, str]] | None:
    """読みを表記の非カナ部分(漢字等)へ割り付ける。

    表記をカナ/非カナのランに分け、カナランはリテラル・非カナランは「1文字以上の
    任意」として組んだ正規表現を読み全体にfullmatchさせる。返すのは非カナランごとの
    (表記の開始idx, 終了idx, そのランの読み)。素のカタカナ化で一致しなければ長音の
    表記ゆれを吸収して再試行し、それでも対応づけできなければNone(呼び出し側は
    単語全体ルビにフォールバックする)。

    読みを持たない記号(中黒等)はカナ側のランに含めたうえで、リテラルからは
    除去する(読みには現れないため)。

    例: 「燦花シノノ」×「サンカシノノ」→ [(0, 2, "サンカ")]
        「アテル＝参」×「アテルサン」→ [(4, 5, "サン")]
    """
    if not surface or not kana:
        return None
    runs = _kana_runs(surface)
    non_kana_runs = [run for run in runs if not run[2]]
    has_kana_anchor = any(
        _strip_silent(surface[start:end])
        for start, end, is_kana in runs
        if is_kana
    )
    if len(non_kana_runs) > 1 and not has_kana_anchor:
        # 「柳瀬 泰平」のように漢字列が空白だけで分かれ、読み側に境界情報が
        # ない名前は各列へ正しく配分できない。誤った部分ルビより全体ルビに戻す。
        return None
    surface_kata = _to_katakana(surface)
    kana_kata = _to_katakana(kana)

    def _match(normalize: bool) -> re.Match[str] | None:
        pattern = ""
        for s, e, is_kana in runs:
            if is_kana:
                # 読みを持たない記号(中黒等)は読みに現れないのでリテラルから外す
                part = _strip_silent(surface_kata[s:e])
                pattern += re.escape(normalize_long_vowels(part) if normalize else part)
            else:
                pattern += "(.+?)"
        target = normalize_long_vowels(kana_kata) if normalize else kana_kata
        return re.fullmatch(pattern, target)

    m = _match(False) or _match(True)
    if m is None:
        return None
    # normalize_long_vowels は1文字を1文字に置き換えるので、マッチ位置は元の読みと揃う
    segments: list[tuple[int, int, str]] = []
    group = 0
    for s, e, is_kana in runs:
        if is_kana:
            continue
        group += 1
        gs, ge = m.span(group)
        segments.append((s, e, kana[gs:ge]))
    return segments


LIBASS_FONT_SIZE_COEFF = 0.72
ASS_MEASURE_SCALE = 64


def _ass_text_width(font_path: Path | None, ass_fontsize: int, text: str) -> float:
    """libassと同じ字送り幅をPillowで小数精度まで測る。

    libassはVSFilter互換の固定係数0.72をASS Fontsizeへ掛けてFreeTypeへ渡す。
    Pillowは整数pxしか指定できないため、64倍で測って縮小し、丸め誤差が行端へ
    累積しないようにする。
    """
    font = _font(font_path, max(1, ass_fontsize * ASS_MEASURE_SCALE))
    return (
        font.getlength(text)
        * LIBASS_FONT_SIZE_COEFF
        / ASS_MEASURE_SCALE
    )


def _ruby_events(
    el: SubtitleElement,
    name: str,
    layer: int,
    start: float,
    end: float,
    words: list[ParodyWord],
    px: float,
    py: float,
    an: int,
    height: int,
    font_path: Path | None,
    max_width: float,
) -> list[str]:
    """替え歌本文を単語ごとに置き、その真上にルビを置くASSイベント列。

    本文とルビを同じx中心の独立したASSイベントとして描くため、行全体の幅に
    小さな測定誤差があっても、ルビは対象語からずれない。部分ルビだけは単語内の
    相対幅を使う。本文と同一レイヤー・同一区間で、範囲ごとに小さいフォントの
    別イベントを本文の上端すぐ上に \\pos で配置する。
    ルビは表記の非カナ部分にだけ振る(_ruby_segments)。読みを割り付けられない
    単語だけ、従来どおり単語全体に読み全体を置く。
    """
    base_body_px = int(el.size * height)
    body_px = base_body_px
    if body_px <= 0 or not words:
        return []
    full = WORD_SEP.join(w.surface for w in words)
    while body_px > 1 and _ass_text_width(font_path, body_px, full) > max_width:
        body_px -= 1
    full = WORD_SEP.join(w.surface for w in words)
    total_w = _ass_text_width(font_path, body_px, full)
    # 本文行の左端x。build_ass本体の px(align基準点)と揃える
    if el.align == "left":
        x0 = px
    elif el.align == "right":
        x0 = px - total_w
    else:
        x0 = px - total_w / 2
    # 本文行の上端y(\pos の基準点 py と valign(an)から逆算。行高は概ねフォントpx)
    if an in (1, 2, 3):
        top = py - body_px
    elif an in (4, 5, 6):
        top = py - body_px / 2
    else:
        top = py
    ruby_px = max(1, round(el.ruby_size * body_px))
    body_an = {1: 2, 2: 2, 3: 2, 4: 5, 5: 5, 6: 5, 7: 8, 8: 8, 9: 8}[an]
    events: list[str] = []
    prefix = ""
    for i, w in enumerate(words):
        if i:
            prefix += WORD_SEP
        start_x = _ass_text_width(font_path, body_px, prefix)
        prefix += w.surface
        end_x = _ass_text_width(font_path, body_px, prefix)
        size_override = f"\\fs{body_px}" if body_px != base_body_px else ""
        # 混在語もランごとに本文を置く。「ペルシャ湾」の「湾」など部分ルビの
        # 対象本文とルビへ、推定ではなく同じ中心座標を指定できる。
        for run_start, run_end, _is_kana in _kana_runs(w.surface):
            run_text = w.surface[run_start:run_end]
            if not _strip_silent(run_text):
                continue
            sx = start_x + _ass_text_width(font_path, body_px, w.surface[:run_start])
            ex = start_x + _ass_text_width(font_path, body_px, w.surface[:run_end])
            run_cx = x0 + (sx + ex) / 2
            events.append(
                f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{name},,0,0,0,,"
                f"{{\\an{body_an}\\pos({run_cx:.2f},{py:.0f}){size_override}}}"
                f"{_ass_escape(run_text)}"
            )
        if not _needs_ruby(w.surface, w.kana):
            continue
        # 表記のカナ部分(そのまま読める部分)にはルビを振らず、漢字等のランごとに
        # 対応する読みだけをその真上へ置く。対応づけできない語は単語全体に読み全体
        segments = _ruby_segments(w.surface, w.kana)
        if segments is None:
            spans = [(start_x, end_x, w.kana)]
        else:
            spans = []
            for s, e, reading in segments:
                if _to_katakana(w.surface[s:e]) == reading:
                    continue  # 表記どおりの読みならルビ不要
                spans.append((
                    start_x + _ass_text_width(font_path, body_px, w.surface[:s]),
                    start_x + _ass_text_width(font_path, body_px, w.surface[:e]),
                    reading,
                ))
        for sx, ex, reading in spans:
            cx = x0 + (sx + ex) / 2  # ルビを振る範囲の中心x
            # \an2: ルビの下端中央をその範囲の中心・本文上端に合わせる(本文のすぐ上に載る)
            events.append(
                f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},{name},Ruby,0,0,0,,"
                f"{{\\an2\\pos({cx:.2f},{top:.0f})\\fs{ruby_px}}}"
                f"{_ass_escape(_to_hiragana(reading))}"
            )
    return events


def build_ass(
    project: Project,
    width: int,
    height: int,
    font: str,
    layout: Layout | None = None,
    granularity: dict[str, str] | None = None,
) -> str:
    """歌詞字幕(替え歌/元歌詞)のASSを作る。行の歌唱区間で表示する。

    消灯はその行の最後の単語画像の余韻(frame_show_end)に合わせる(次の行の
    表示が始まればそこで交代)。位置・サイズ・色はレイアウトのsubtitle要素から
    決める。subtitle要素のないレイアウトでは既定(下部2段: 上=替え歌、下=元歌詞)になる。
    表示粒度(行/フレーズ)は subtitle要素の granularity、なければ granularity 引数
    (Web UIの一括指定)、それも無ければ source 既定に従う。
    """
    from .align import build_subtitle_segments, resolve_granularity

    subs = layout.subtitles if layout and layout.subtitles else DEFAULT_SUBTITLES
    # スタイル名はsource由来(Parody/Original)。同一sourceが複数あれば連番を足す
    names: list[str] = []
    for el in subs:
        base = el.source.capitalize()
        name = base if base not in names else f"{base}{sum(n.startswith(base) for n in names) + 1}"
        names.append(name)
    styles = []
    for el, name in zip(subs, names, strict=True):
        styles.append(
            f"Style: {name},{font},{int(el.size * height)},{_ass_color(el.color)},"
            f"&H000000FF,&H00202020,&H96000000,{-1 if el.bold else 0},0,0,0,"
            f"100,100,0,0,1,2,1,{_ass_alignment(el)},0,0,0,1"
        )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{chr(10).join(styles)}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    parody_lines = {pl.line_id: pl for pl in project.parody.lines} if project.parody else {}
    # 先に全行の表示区間を決め、前後の行と重ならないようにする。
    # 同時に表示される字幕があるとASSレンダラの衝突回避が働いて
    # 字幕が上に積み上がり、行の切り替わりで位置が跳ねるため
    shown = [line for line in project.lines if line.note_ids]
    # 行の消灯は単語画像の余韻(frame_show_end)に合わせる。間奏の手前で
    # 字幕だけ先に消えて画像が残る、というずれをなくすため。
    # 単語フレームが無い行(画像なしレイアウト等)は従来のパディングのみ
    show_ends = line_show_ends(project, layout or load_layout(None))
    spans = []
    for line in shown:
        start, end = project.line_time_range(line)
        stop = max(end + SUB_PAD_SEC, show_ends.get(line.id, end))
        spans.append([start - SUB_PAD_SEC, stop])
    for j in range(len(spans) - 1):
        spans[j][1] = min(spans[j][1], spans[j + 1][0])
        spans[j][1] = max(spans[j][1], spans[j][0] + 0.2)  # 行の重なりが極端でも一瞬は出す
        spans[j + 1][0] = max(spans[j + 1][0], spans[j][1])

    font_path = resolve_font_path(layout.font if layout else None)
    # 行ごとの素材(グループ化・切り出し・マージは align 側の共通ロジックで行う)
    plines = [parody_lines.get(line.id) for line in shown]
    originals = [line.original_text for line in shown]  # グループ化キー(未対応はNone)
    # 元歌詞のフレーズ切り出しは読み(かな)どうしで突き合わせるので XFカナを優先
    xf_texts = [line.xf_kana or line.xf_surface for line in shown]
    original_full = [(line.original_text or line.xf_surface) for line in shown]
    parody_full = [
        WORD_SEP.join(w.surface for w in pl.words) if pl and pl.words else ""
        for pl in plines
    ]
    # spans は上の重なり調整で可変listにしていたので、区間は (start, end) に固める
    span_pairs = [(s[0], s[1]) for s in spans]

    events = []
    for el, name in zip(subs, names, strict=True):
        gran = resolve_granularity(el.source, getattr(el, "granularity", None), granularity)
        full_texts = parody_full if el.source == "parody" else original_full
        segments = build_subtitle_segments(
            el.source, gran, originals, full_texts, xf_texts, span_pairs, sep=WORD_SEP
        )
        # \posで固定配置(boxのalign/valign側の辺が基準点)。
        # レイヤーをsourceで分けておくと、万一区間が重なっても替え歌と
        # 元歌詞が衝突回避で入れ替わらない(衝突判定は同一レイヤー内のみ)
        layer = 1 if el.source == "parody" else 0
        an = _ass_alignment(el)
        x, y, w, h = el.box
        px = {"left": x, "right": x + w}.get(el.align, x + w / 2) * width
        py = {"top": y, "middle": y + h / 2}.get(el.valign, y + h) * height
        for seg in segments:
            if not seg.text:
                continue
            # ルビ(ふりがな): 替え歌字幕のみ。本文と同一レイヤー・同一区間で、
            # 本文も単語ごとの別イベントにしてルビと同じ中心へ置く。
            # 行マージ(parody=line)時はグループ内の全単語を連結して並べる。
            if el.source == "parody" and el.ruby:
                words = []
                for k in seg.indices:
                    pl = plines[k]
                    if pl is not None:
                        words.extend(pl.words)
                if words:
                    events.extend(
                        _ruby_events(
                            el, name, layer, seg.start, seg.end, words, px, py, an,
                            height, font_path, w * width,
                        )
                    )
                    continue
            events.append(
                f"Dialogue: {layer},{_ass_time(seg.start)},{_ass_time(seg.end)},{name},,0,0,0,,"
                f"{{\\an{an}\\pos({px:.0f},{py:.0f})}}{_ass_escape(seg.text)}"
            )
    return header + "\n".join(events) + "\n"


# ---- クレジット ----


def write_credits(credits: list[dict], work: Path) -> Path | None:
    if not credits:
        return None
    lines = [
        "# 画像クレジット",
        "",
        "この動画で使用した画像の出典。公開時は各ファイルページのライセンス"
        "(作者表示など)に従ってください。",
        "クレジット欄が空の画像は表記不要(パブリックドメイン等)か情報を取得"
        "できなかったもので、後者はライセンス確認先で要確認です。",
        "",
        "| 単語 | 画像 | クレジット | ライセンス確認先 |",
        "|---|---|---|---|",
    ]
    for c in credits:
        lines.append(
            f"| {c['original']} | {c['image']} | {c.get('credit', '')} | {c['image_page']} |"
        )
    path = work / "credits.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- 本体 ----


@dataclass(frozen=True)
class PreparedVideo:
    """エンコード直前まで用意した映像素材。音声側と独立して生成できる。"""

    work: Path
    concat_path: Path
    ass_path: Path
    total_sec: float
    fps: int


def _sung_end_sec(project: Project) -> float:
    return max((n.end_sec for n in project.notes), default=0.0) + 3.0


def actual_video_total_sec(project: Project, audio_path: Path) -> float:
    """完成音声を基準に、従来のmake_videoと同じ最終尺を返す。"""
    sung_end = _sung_end_sec(project)
    total = _resolve_total_sec(sung_end, _audio_duration_sec(audio_path))
    return extend_for_endroll(total, sung_end, used_words(project))


def planned_video_total_sec(project: Project) -> float:
    """音声完成前に安全側で見積もる無音動画の尺。

    MIDI伴奏はfluidsynthのリバーブ等でイベント終端より数秒長くなる。
    実機で最大約4.8秒だったため6秒の余裕を持たせる。完成音声がそれでも
    長かった場合はAPI側が従来の直列生成へフォールバックする。
    """
    sung_end = _sung_end_sec(project)
    expected_audio: float | None = None
    accompaniment = project.song.accompaniment_path
    if accompaniment:
        path = Path(accompaniment)
        if path.exists():
            expected_audio = _audio_duration_sec(path)
    elif project.song.midi_path:
        try:
            import mido

            expected_audio = float(mido.MidiFile(project.song.midi_path, clip=True).length) + 6.0
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("MIDIから動画予定尺を取得できませんでした: %s", exc)
    total = _resolve_total_sec(sung_end, expected_audio)
    return extend_for_endroll(total, sung_end, used_words(project))


def prepare_video(
    project: Project,
    project_dir: Path,
    total_sec: float,
    width: int = 1280,
    height: int = 720,
    font: str = "Hiragino Sans",
    image_cache: Path | None = None,
    layout: str | None = None,
    granularity: dict[str, str] | None = None,
    song_title: str | None = None,
    synth_credit: str = "",
    song_title_kana: str = "",
    fps: int = DEFAULT_VIDEO_FPS,
    original_credit: str = "",
    credit_notice: str = "",
    image_lead_sec: float = DEFAULT_IMAGE_LEAD_SEC,
) -> PreparedVideo:
    """画像・字幕・concatを準備する。音声ファイルには一切依存しない。"""
    if fps <= 0:
        raise ValueError("fps は1以上で指定してください")
    layout_obj = load_layout(layout)
    credit_text = app_credit_text(synth_credit, original_credit, credit_notice)
    work = project_dir / VIDEO_DIR
    work.mkdir(parents=True, exist_ok=True)

    sung_end = _sung_end_sec(project)
    minimum = extend_for_endroll(sung_end, sung_end, used_words(project))
    if total_sec + 1e-6 < minimum:
        raise ValueError(f"動画予定尺が短すぎます({total_sec:.3f} < {minimum:.3f})")
    if total_sec > sung_end:
        logger.info("動画予定尺: %.1f秒 (歌唱終端+余韻 %.1f秒)", total_sec, sung_end)

    prepare_started = time.monotonic()
    cues, credits = build_image_cues(
        project, work, width, height, image_cache, layout_obj, credit_text,
        image_lead_sec=image_lead_sec,
    )
    if cues:
        logger.info("画像キュー: %d件", len(cues))
    else:
        logger.warning("画像キューが0件です。動画の背景は全編無地になります")
    thumbnail = generate_thumbnail(
        project,
        project_dir,
        width,
        height,
        image_cache,
        song_title,
        credit_text,
        title_kana=song_title_kana,
    )
    if thumbnail is not None:
        cues = prepend_thumbnail_cue(cues, thumbnail, thumbnail_show_end(project))
    section_cues = build_section_cues(
        project, cues, total_sec, layout_obj, work, width, height, credit_text, credits,
        synth_credit=synth_credit,
        original_song=(song_title or Path(project.song.midi_path).stem).strip(),
        original_credit=original_credit.strip(),
        credit_notice=credit_notice.strip(),
    )
    if section_cues:
        logger.info("間奏・後奏のフレーム: %d件", len(section_cues))
        cues = sorted([*cues, *section_cues], key=lambda c: c.start)
    idle_frame = render_idle_frame(
        layout_obj, idle_frame_data(project, credit_text), width, height, work / "frames"
    )
    concat_path = _write_slideshow_concat(
        cues, work, width, height, total_sec, idle_frame
    )
    ass_path = work / "subtitles.ass"
    ass_path.write_text(
        build_ass(project, width, height, font, layout_obj, granularity), encoding="utf-8"
    )
    credits_path = write_credits(credits, work)
    if credits_path:
        runproc.log_generated_path(logger, "画像クレジットを書き出しました", credits_path)
    logger.info("動画前処理完了: %.1f秒", time.monotonic() - prepare_started)
    return PreparedVideo(work, concat_path, ass_path, total_sec, fps)


def _ass_filter_arg(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace(
        "'", "\\'"
    )


def encode_silent_video(prepared: PreparedVideo) -> Path:
    """音声なしのH.264映像を作る。歌声合成・ミックスと並列実行できる。"""
    out = prepared.work / "video-only.mp4"
    started = time.monotonic()
    logger.info("無音動画エンコード開始: 1パス / %dfps", prepared.fps)
    _run(
        [_ffmpeg(), "-y",
         "-f", "concat", "-safe", "0", "-i", str(prepared.concat_path),
         "-vf", f"fps={prepared.fps},format=yuv420p,subtitles='{_ass_filter_arg(prepared.ass_path)}'",
         "-an", "-c:v", "libx264", "-preset", "fast",
         "-t", f"{prepared.total_sec:.3f}", str(out)],
        "無音動画の生成",
    )
    logger.info("無音動画エンコード完了: %.1f秒", time.monotonic() - started)
    return out


def attach_audio(
    silent_video: Path,
    audio_path: Path,
    total_sec: float,
    out: Path | None = None,
) -> Path:
    """H.264を再エンコードせず、AAC音声だけを追加して完成MP4を作る。"""
    target = out or silent_video.with_name("out.mp4")
    started = time.monotonic()
    logger.info("音声結合開始: 映像stream copy / 音声AAC")
    _run(
        [_ffmpeg(), "-y", "-i", str(silent_video), "-i", str(audio_path),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", "apad",
         "-t", f"{total_sec:.3f}", "-movflags", "+faststart", str(target)],
        "動画と音声の結合",
    )
    logger.info("音声結合完了: %.1f秒", time.monotonic() - started)
    return target


def make_video(
    project: Project,
    project_dir: Path,
    width: int = 1280,
    height: int = 720,
    font: str = "Hiragino Sans",
    audio: str | None = None,
    image_cache: Path | None = None,
    layout: str | None = None,
    granularity: dict[str, str] | None = None,
    song_title: str | None = None,
    synth_credit: str = "",
    song_title_kana: str = "",
    fps: int = DEFAULT_VIDEO_FPS,
    original_credit: str = "",
    credit_notice: str = "",
    image_lead_sec: float = DEFAULT_IMAGE_LEAD_SEC,
) -> Path:
    if fps <= 0:
        raise ValueError("fps は1以上で指定してください")
    layout_obj = load_layout(layout)
    # 動画に焼き込むクレジット(サムネ・単語フレーム・idleで共通)
    credit_text = app_credit_text(synth_credit, original_credit, credit_notice)
    work = project_dir / VIDEO_DIR
    work.mkdir(parents=True, exist_ok=True)

    audio_path: Path | None = Path(audio) if audio else None
    if audio_path is None:
        for candidate in (project_dir / MIX_DIR / "song.wav",
                          project_dir / NEUTRINO_DIR / "vocal.wav"):
            if candidate.exists():
                audio_path = candidate
                break
    if audio_path is None or not audio_path.exists():
        raise RuntimeError(
            "音声がありません。先に mix(または synthesize)を実行するか --audio で指定してください"
        )

    sung_end_sec = max(n.end_sec for n in project.notes) + 3.0
    total_sec = _resolve_total_sec(sung_end_sec, _audio_duration_sec(audio_path))
    # 後奏が短い曲は末尾に時間を足してエンドロール枠を作る(足したぶんは無音)
    extended_sec = extend_for_endroll(total_sec, sung_end_sec, used_words(project))
    if extended_sec > total_sec:
        logger.info(
            "後奏が短いため動画を%.1f秒延長してエンドロールを出します",
            extended_sec - total_sec,
        )
        total_sec = extended_sec

    prepare_started = time.monotonic()
    cues, credits = build_image_cues(
        project, work, width, height, image_cache, layout_obj, credit_text,
        image_lead_sec=image_lead_sec,
    )
    if cues:
        logger.info("画像キュー: %d件", len(cues))
    else:
        # 画像取得の全滅(ネットワーク・レート制限)や、画像もテキストも無い
        # レイアウトで起きる。動画は生成されるが全編無地になるので目立たせる
        logger.warning("画像キューが0件です。動画の背景は全編無地になります")
    # 曲名の空耳変換つきサムネ(thumbnail.png)。前奏区間に出すほか、SNS投稿用に
    # ジョブディレクトリへ残す。生成に失敗しても動画は作る(サムネ無しになるだけ)。
    # song_title_kana は曲名の読み(分かっていれば変換入力に使う)
    thumbnail = generate_thumbnail(
        project,
        project_dir,
        width,
        height,
        image_cache,
        song_title,
        credit_text,
        title_kana=song_title_kana,
    )
    if thumbnail is not None:
        cues = prepend_thumbnail_cue(cues, thumbnail, thumbnail_show_end(project))
    # 間奏の「間奏(X秒)」・後奏のエンドロールを、歌唱フレームの隙間に差し込む
    section_cues = build_section_cues(
        project, cues, total_sec, layout_obj, work, width, height, credit_text, credits,
        synth_credit=synth_credit,
        original_song=(song_title or Path(project.song.midi_path).stem).strip(),
        original_credit=original_credit.strip(),
        credit_notice=credit_notice.strip(),
    )
    if section_cues:
        logger.info("間奏・後奏のフレーム: %d件", len(section_cues))
        cues = sorted([*cues, *section_cues], key=lambda c: c.start)
    # 残った隙間(短い間奏・専用定義の無い区間)用のidleフレーム(定義があるときだけ)
    idle_frame = render_idle_frame(
        layout_obj, idle_frame_data(project, credit_text), width, height, work / "frames"
    )
    concat_path = _write_slideshow_concat(
        cues, work, width, height, total_sec, idle_frame
    )

    ass_path = work / "subtitles.ass"
    ass_path.write_text(
        build_ass(project, width, height, font, layout_obj, granularity), encoding="utf-8"
    )

    credits_path = write_credits(credits, work)
    if credits_path:
        runproc.log_generated_path(logger, "画像クレジットを書き出しました", credits_path)
    logger.info("動画前処理完了: %.1f秒", time.monotonic() - prepare_started)

    out = work / "out.mp4"
    # subtitlesフィルタのパスはffmpegのフィルタ構文でエスケープが要る
    ass_arg = str(ass_path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace(
        "'", "\\'"
    )
    # 総尺は常に total_sec に揃える。音声が短い(エンドロール用に映像を延ばした)
    # ときは apad で無音を足し、音声が長いときは -t で切る(-shortest だと映像側が
    # 音声に合わせて切られ、足したエンドロールが消えてしまう)
    encode_started = time.monotonic()
    logger.info("動画エンコード開始: 1パス / %dfps", fps)
    _run(
        [_ffmpeg(), "-y",
         "-f", "concat", "-safe", "0", "-i", str(concat_path),
         "-i", str(audio_path),
         "-vf", f"fps={fps},format=yuv420p,subtitles='{ass_arg}'",
         "-af", "apad",
         "-c:v", "libx264", "-preset", "fast",
         "-c:a", "aac", "-b:a", "192k",
         "-t", f"{total_sec:.3f}",
         str(out)],
        "動画の最終合成",
    )
    logger.info("動画エンコード完了: %.1f秒", time.monotonic() - encode_started)
    return out
