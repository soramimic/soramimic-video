"""生成前に出す「仮サムネ」(おまかせ確認モーダルのプレビュー)の生成とキャッシュ。

ジョブを走らせる前に、その組み合わせ(サンプル曲 × 単語リスト)で実際に
作られるサムネの近似をモーダルに出すためのもの。描画は thumbnail.py の
build_thumbnail をそのまま使う(コードは重複させない)ので、見た目は本番の
サムネと同じトーンになる。

**軽さが最優先**なので、本番のサムネ生成とは次の点だけ意図的に変えている:

* 画像はキャッシュ済みのぶんだけ使う(download_images=False)。まだ落として
  いない画像は待たずに諦め、【言い換え】+文字だけのサムネにする。そのぶんは
  裏で先読みし、取れたらキャッシュを捨てて次に開くときは絵入りにする
* 解像度はモーダル表示に足りる 640x360(本番は1280x720)

生成結果は (曲名, 単語リストCSVの内容, where, 変換パラメータ, 解像度,
レイアウト定義) のハッシュをキーにPNGをディスクキャッシュする。2回目以降は
変換を走らせずそのまま返す。キャッシュはTTLと件数上限で刈る。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .convert import resolve_convert_settings, resolve_wordlist
from .thumbnail import (
    BACKGROUND_DIM,
    DEFAULT_STYLE,
    HEADLINE_MAX_WORDS,
    HEADLINE_MIN_CHARS,
    build_thumbnail,
    thumbnail_layout_spec,
    wordlist_text_of,
)

logger = logging.getLogger(__name__)

# モーダルに出すだけなので本番(1280x720)より小さくてよい。描画も軽くなる
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
CACHE_DIRNAME = "thumbnail-preview-cache"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # これより古いPNGは捨てる
CACHE_MAX_ENTRIES = 300  # 件数上限(超過ぶんは古い順に捨てる)
# 生成は同時に1本だけ通す(変換はCPUを食うので連打で並列に走らせない)。
# 待たされ続けるくらいなら諦めてもらう(UIは代表画像にフォールバックする)
RENDER_TIMEOUT_SECONDS = 15.0
MAX_PREFETCH_IMAGES = 4  # 1回のプレビューで裏読みする画像の上限(見出しは最大2語)

# 短期レート制限(セッションあたり)。ジョブではないので日次クォータは消費しないが、
# 連打・スクレイピングで変換が走り続けないようにする。0 以下で無効。
RATE_LIMIT_ENV = "SORAMIMIC_PREVIEW_RATE_LIMIT"
RATE_WINDOW_ENV = "SORAMIMIC_PREVIEW_RATE_WINDOW"
DEFAULT_RATE_LIMIT = 10
DEFAULT_RATE_WINDOW = 60.0


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("環境変数 %s の値が数値ではありません: %r", name, raw)
        return default


def preview_cache_dir(jobs_dir: Path) -> Path:
    """プレビューPNGのキャッシュ先(単語画像キャッシュと同じくジョブ置き場の直下)。"""
    return jobs_dir.resolve() / CACHE_DIRNAME


def _layout_fingerprint() -> str:
    """サムネの見た目を決めるものの指紋。デザインを変えたらキャッシュが自動で無効になる。"""
    specs = [
        thumbnail_layout_spec(has_word=True, has_image=True),
        thumbnail_layout_spec(has_word=True, has_image=False),
        thumbnail_layout_spec(has_word=False, has_image=False),
    ]
    return json.dumps(
        [DEFAULT_STYLE, BACKGROUND_DIM, HEADLINE_MAX_WORDS, HEADLINE_MIN_CHARS, specs],
        ensure_ascii=False,
        sort_keys=True,
    )


@dataclass(frozen=True)
class PreviewSpec:
    """プレビュー1枚ぶんの実効パラメータ(+キャッシュキー)。"""

    title: str
    wordlist: str
    csv_path: Path
    wordlist_text: str
    where: str | None
    params: dict[str, Any] = field(default_factory=dict)
    width: int = PREVIEW_WIDTH
    height: int = PREVIEW_HEIGHT
    # 単語画像を貼るか。Falseなら文字だけのサムネにする(昆虫など、画像を
    # 初期非表示にしている単語リスト向け。index.html の HIDDEN_PREVIEW_WORDLISTS)
    with_images: bool = True

    @classmethod
    def create(
        cls,
        title: str,
        wordlist: str,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        width: int = PREVIEW_WIDTH,
        height: int = PREVIEW_HEIGHT,
        with_images: bool = True,
    ) -> PreviewSpec:
        """where・変換パラメータの既定をジョブ本体と同じ経路で解決して組み立てる。

        単語リストが見つからないときは FileNotFoundError(呼び出し側で404にする)。
        """
        csv_path = resolve_wordlist(wordlist)
        eff_where, coerced, _alpha = resolve_convert_settings(csv_path, where, params)
        return cls(
            title=title,
            wordlist=wordlist,
            csv_path=csv_path,
            wordlist_text=wordlist_text_of(wordlist),
            where=eff_where,
            params=coerced,
            width=width,
            height=height,
            with_images=with_images,
        )

    @property
    def key(self) -> str:
        """キャッシュキー。描画結果を変えうるものはすべて入れる。

        単語リストはパスだけでなくCSVの mtime・サイズも見るので、リストを
        更新すれば作り直される(soramimic_engine の db_cache_key と同じ流儀)。
        """
        st = self.csv_path.stat()
        payload = json.dumps(
            {
                "title": self.title,
                "csv": str(self.csv_path),
                "csv_mtime_ns": st.st_mtime_ns,
                "csv_size": st.st_size,
                "wordlist_text": self.wordlist_text,
                "where": self.where or "",
                "params": {k: str(v) for k, v in self.params.items()},
                "size": [self.width, self.height],
                "with_images": self.with_images,
                "layout": _layout_fingerprint(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]

    def cached(self, cache_dir: Path) -> Path | None:
        """生成済みのプレビューPNG(無ければ None)。"""
        path = cache_dir / f"{self.key}.png"
        return path if path.exists() else None

    def render(self, cache_dir: Path, image_cache: Path | None = None) -> Path | None:
        """プレビューPNGを生成してキャッシュに入れる(描画に失敗したら None)。

        画像はキャッシュ済みのものだけを使い、ダウンロードは待たない。
        with_images=False なら単語画像は一切貼らない(先読みもしない)。
        """
        if not self.with_images:
            image_cache = None
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{self.key}.png"
        # 書きかけを他のリクエストに読ませないよう、一時ファイルに描いてから置換する
        tmp = cache_dir / f".{self.key}.{os.getpid()}.tmp.png"
        started = time.monotonic()
        missing: list[tuple[str, str]] = []
        out = build_thumbnail(
            tmp,
            self.title,
            self.wordlist,
            where=self.where,
            params=self.params,
            image_cache=image_cache,
            width=self.width,
            height=self.height,
            download_images=False,
            missing_images=missing,
        )
        if out is None:
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, path)
        logger.info(
            "サムネプレビューを生成しました: %s × %s (%.1f秒)",
            self.title, self.wordlist, time.monotonic() - started,
        )
        prune_cache(cache_dir)
        if missing and image_cache is not None:
            # 今回は画像なしで返したが、次に開くときは絵入りにできるよう裏で温める
            # (取れたらこのPNGを捨てて作り直させる。取れなければ何もしない)
            start_image_prefetch(missing, image_cache, path)
        return path


_prefetching: set[str] = set()
_prefetch_lock = threading.Lock()


def prefetch_images(
    items: Sequence[tuple[str, str]], image_cache: Path, invalidate: Path
) -> int:
    """単語画像とクレジットをキャッシュに落とし、1つでも取れたら invalidate を捨てる。

    プレビューは待てないので画像なしで返しているが、次に同じ組み合わせを開いた
    ときには絵入り(+必要なクレジット表記付き)にしたい。取れなければ何もしない
    (=画像なしのPNGが残るので、通信できない環境で再生成を繰り返すことはない)。
    items は (画像URL, 画像ページURL) の列。
    """
    from .image_credit import fetch_image_credit
    from .video import cached_image, download_image

    got = 0
    for url, page in list(dict.fromkeys(items))[:MAX_PREFETCH_IMAGES]:
        try:
            had_image = cached_image(url, image_cache) is not None
            if download_image(url, image_cache) is not None and not had_image:
                got += 1
            # 表記が要る画像(Commons)ではクレジットも温める。表記不要・対象外はNone
            if fetch_image_credit(url, page, image_cache) is not None:
                got += 1
        except Exception:  # noqa: BLE001 - 先読みの失敗は無視してよい
            logger.warning("サムネプレビュー用の画像を先読みできません: %s", url)
    if got:
        invalidate.unlink(missing_ok=True)
        logger.info("画像が揃ったのでサムネプレビューを作り直させます: %s", invalidate.name)
    return got


def start_image_prefetch(
    urls: Sequence[tuple[str, str]], image_cache: Path, invalidate: Path
) -> threading.Thread | None:
    """prefetch_images をdaemonスレッドで走らせる(同じPNGに対しては1本だけ)。"""
    key = invalidate.name
    with _prefetch_lock:
        if key in _prefetching:
            return None
        _prefetching.add(key)

    def run() -> None:
        try:
            prefetch_images(urls, image_cache, invalidate)
        finally:
            with _prefetch_lock:
                _prefetching.discard(key)

    thread = threading.Thread(target=run, name="thumbnail-preview-prefetch", daemon=True)
    thread.start()
    return thread


def prune_cache(
    cache_dir: Path,
    max_entries: int = CACHE_MAX_ENTRIES,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """キャッシュを刈る(TTL超過 → それでも多すぎるぶんを古い順に)。消したPNGを返す。"""
    now = now if now is not None else time.time()
    try:
        entries = [(p.stat().st_mtime, p) for p in cache_dir.glob("*.png")]
    except OSError:
        return []
    entries.sort()
    removed: list[Path] = []
    keep: list[tuple[float, Path]] = []
    for mtime, path in entries:
        if ttl_seconds > 0 and now - mtime > ttl_seconds:
            removed.append(path)
        else:
            keep.append((mtime, path))
    if max_entries > 0 and len(keep) > max_entries:
        removed.extend(p for _, p in keep[: len(keep) - max_entries])
    for path in removed:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - 同時に消されただけなら無視してよい
            pass
    if removed:
        logger.info("サムネプレビューのキャッシュを%d件削除しました", len(removed))
    return removed


class RateLimiter:
    """セッション(取れなければIP)ごとの短期レート制限。

    「直近 window 秒に limit 回まで」の素朴なスライディングウィンドウ。
    プレビューはジョブではないので日次クォータは消費しないが、連打で変換が
    走り続けないようにここで止める。キャッシュヒットは数えない(コストが無いため)。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """1回ぶん記録して、上限内なら True。超過なら記録せず False。"""
        limit = int(_env_number(RATE_LIMIT_ENV, DEFAULT_RATE_LIMIT))
        window = _env_number(RATE_WINDOW_ENV, DEFAULT_RATE_WINDOW)
        if limit <= 0 or window <= 0:
            return True
        now = time.time() if now is None else now
        cutoff = now - window
        with self._lock:
            # 使われなくなったセッションのエントリを溜めない
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
                del self._hits[k]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


_render_lock = threading.Lock()


@contextmanager
def render_slot(timeout: float = RENDER_TIMEOUT_SECONDS) -> Iterator[None]:
    """プレビュー生成の実行枠(同時に1本)。取れなければ TimeoutError。"""
    if not _render_lock.acquire(timeout=timeout):
        raise TimeoutError("サムネプレビューの生成が混み合っています")
    try:
        yield
    finally:
        _render_lock.release()
