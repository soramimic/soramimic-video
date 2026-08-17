"""生成前に出す「仮サムネ」(おまかせ確認モーダルのプレビュー)の生成とキャッシュ。

ジョブを走らせる前に、その組み合わせ(サンプル曲 × 単語リスト)で実際に
作られるサムネの近似をモーダルに出すためのもの。描画は thumbnail.py の
build_thumbnail をそのまま使う(コードは重複させない)ので、見た目は本番の
サムネと同じトーンになる。

**軽さが最優先**なので、本番のサムネ生成とは次の点だけ意図的に変えている:

* 画像はキャッシュ済みのぶんだけ使う(download_images=False)。ただし初見の
  1回目から絵入りを出したいので、足りないぶんは IMAGE_WAIT_SECONDS 秒だけ
  待つ。それでも間に合わなければ【言い換え】+文字だけのサムネで返し、裏で
  取り切ってから同じキャッシュキーのPNGを絵入りに作り直す(UIは
  X-Preview-Images: pending を見て数秒後に1回だけ取り直し、静かに差し替える。
  作り直し済みなのでその取り直しはキャッシュヒット=生成miss枠を消費しない)
* 解像度はモーダル表示に足りる 640x360(本番は1280x720)

画像を初期非表示にしている単語リスト(index.html の HIDDEN_PREVIEW_WORDLISTS。
昆虫など)では、モーダルが with_images=False で頼むので単語画像を一切貼らず、
先読みもしない(「画像を表示する」を押されたときだけ画像入りで作り直す)。

生成結果は (曲名, 単語リストCSVの内容, where, 変換パラメータ, 解像度,
画像の有無, レイアウト定義) のハッシュをキーにPNGをディスクキャッシュする。
2回目以降は変換を走らせずそのまま返す。キャッシュはTTLと件数上限で刈る。
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
    DEFAULT_STYLE,
    HEADLINE_MAX_WORDS,
    SIGNATURE,
    build_thumbnail,
    design_fingerprint,
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
MAX_PREFETCH_IMAGES = 4  # 1回のプレビューで裏読みする画像の上限(見出しの単語数ぶん)
# 1回目のプレビューで画像のダウンロードを待つ上限(合計)。初見でも間に合えば
# 絵入りで返せる。超えたら文字だけで返し、続きは裏読み+作り直しに回す
IMAGE_WAIT_SECONDS = 2.0
PENDING_SUFFIX = ".pending"  # 「まだ絵が入っていないPNG」の目印(APIのヘッダに出す)

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
    """サムネの見た目を決めるものの指紋。デザインを変えたらキャッシュが自動で無効になる。

    署名は要素側が {app_credit} テンプレートなので、文言(SIGNATURE)を変えても
    spec は変わらない。文言そのものも指紋に入れて古いプレビューが残らないようにする
    (プレビューは常に既定の署名で描く。歌声合成のクレジットはジョブ側でのみ足す)。

    可読性デザイン(TEXT_DESIGN)は背景の暗転・グラデーション・白黒反転のように
    spec に現れない設定を持つので、design_fingerprint() でまとめて入れる。
    """
    specs = [
        thumbnail_layout_spec(has_word=True, has_image=True),
        thumbnail_layout_spec(has_word=True, has_image=False),
        thumbnail_layout_spec(has_word=False, has_image=False),
    ]
    return json.dumps(
        [DEFAULT_STYLE, design_fingerprint(), HEADLINE_MAX_WORDS, SIGNATURE, specs],
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
    # 曲名の読み(カタカナ)。あれば変換の入力に使う(samples.json の title_kana)。
    # 見出しに出す曲名は title のまま
    title_kana: str = ""
    allow_noncommercial_fanwork: bool = False

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
        title_kana: str = "",
        allow_noncommercial_fanwork: bool = False,
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
            title_kana=title_kana,
            allow_noncommercial_fanwork=allow_noncommercial_fanwork,
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
                "title_kana": self.title_kana,
                "csv": str(self.csv_path),
                "csv_mtime_ns": st.st_mtime_ns,
                "csv_size": st.st_size,
                "wordlist_text": self.wordlist_text,
                "where": self.where or "",
                "params": {k: str(v) for k, v in self.params.items()},
                "size": [self.width, self.height],
                "with_images": self.with_images,
                "allow_noncommercial_fanwork": self.allow_noncommercial_fanwork,
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

    def pending_marker(self, cache_dir: Path) -> Path:
        """「このPNGにはまだ絵が入っていない」目印のパス。"""
        return cache_dir / f"{self.key}{PENDING_SUFFIX}"

    def images_pending(self, cache_dir: Path) -> bool:
        """使いたかった画像が間に合っていない(裏で取得中)か。"""
        return self.pending_marker(cache_dir).exists()

    def render(
        self,
        cache_dir: Path,
        image_cache: Path | None = None,
        wait_sec: float | None = None,
        refresh: bool = True,
    ) -> Path | None:
        """プレビューPNGを生成してキャッシュに入れる(描画に失敗したら None)。

        画像は wait_sec 秒(既定 IMAGE_WAIT_SECONDS)だけ待ち、間に合わなかった
        ぶんは諦めて文字だけで返す。
        諦めたぶんは pending の目印を残し、refresh=True なら裏で取り切ってから
        同じPNGを絵入りに作り直す(その作り直し自体は refresh=False で呼ぶ)。
        with_images=False なら単語画像は一切貼らない(待ちも裏読みもしない)。
        """
        if not self.with_images:
            image_cache = None
        wait_sec = IMAGE_WAIT_SECONDS if wait_sec is None else wait_sec
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
            image_wait_sec=wait_sec if image_cache is not None else 0.0,
            song_kana=self.title_kana,
            allow_noncommercial_fanwork=self.allow_noncommercial_fanwork,
        )
        if out is None:
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, path)
        logger.info(
            "サムネプレビューを生成しました: %s × %s (%.1f秒, 画像未取得%d件)",
            self.title, self.wordlist, time.monotonic() - started, len(missing),
        )
        prune_cache(cache_dir)
        marker = self.pending_marker(cache_dir)
        if missing and image_cache is not None:
            # 待っても間に合わなかったぶんは裏で取り切り、絵入りに作り直す
            marker.write_text("", encoding="utf-8")
            if refresh:
                start_image_refresh(self, cache_dir, image_cache, missing)
        else:
            marker.unlink(missing_ok=True)
        return path


_prefetching: set[str] = set()
_prefetch_lock = threading.Lock()
_prefetch_slots = threading.BoundedSemaphore(2)


def prefetch_images(items: Sequence[tuple[str, str]], image_cache: Path) -> int:
    """単語画像とクレジットをキャッシュに落とし、新しく取れた件数を返す。

    プレビューは長くは待てないので画像なしで返していることがある。ここで
    取り切っておけば、同じ組み合わせを絵入り(+必要なクレジット表記付き)で
    作り直せる。items は (画像URL, 画像ページURL) の列。
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
    return got


def refresh_with_images(
    spec: PreviewSpec,
    cache_dir: Path,
    image_cache: Path,
    missing: Sequence[tuple[str, str]],
) -> bool:
    """画像を取り切り、取れたらそのプレビューPNGを絵入りに作り直す(作り直したらTrue)。

    UI側は「絵なし」で返ってきたプレビューを数秒後に取り直すので、ここで
    先に作り直しておけばその取り直しはキャッシュヒットになり、生成miss枠も
    変換もこれ以上消費しない。取れなければ何もしない(=絵なしのPNGが残るので、
    通信できない環境で再生成を繰り返すことはない)。
    """
    if not prefetch_images(missing, image_cache):
        return False
    try:
        with render_slot():
            # 待ちも裏読みもなし(画像はもうキャッシュにある)
            out = spec.render(cache_dir, image_cache, wait_sec=0.0, refresh=False)
    except TimeoutError:
        out = None
    if out is None:
        # 作り直せなかったぶんは捨てて、次に開いたときに作り直させる
        (cache_dir / f"{spec.key}.png").unlink(missing_ok=True)
        spec.pending_marker(cache_dir).unlink(missing_ok=True)
        return False
    logger.info("画像が揃ったのでサムネプレビューを作り直しました: %s", out.name)
    return True


def start_image_refresh(
    spec: PreviewSpec,
    cache_dir: Path,
    image_cache: Path,
    missing: Sequence[tuple[str, str]],
) -> threading.Thread | None:
    """refresh_with_images をdaemonスレッドで走らせる(同じPNGに対しては1本だけ)。"""
    key = spec.key
    if not _prefetch_slots.acquire(blocking=False):
        return None
    with _prefetch_lock:
        if key in _prefetching:
            _prefetch_slots.release()
            return None
        _prefetching.add(key)

    def run() -> None:
        try:
            refresh_with_images(spec, cache_dir, image_cache, missing)
        finally:
            with _prefetch_lock:
                _prefetching.discard(key)
            _prefetch_slots.release()

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
    # PNGが無くなった pending の目印は残さない(ヘッダの判定が狂うため)
    for marker in cache_dir.glob(f"*{PENDING_SUFFIX}"):
        if not marker.with_suffix(".png").exists():
            marker.unlink(missing_ok=True)
    if removed:
        logger.info("サムネプレビューのキャッシュを%d件削除しました", len(removed))
    return removed


class RateLimiter:
    """セッション(取れなければIP)ごとの短期レート制限。

    「直近 window 秒に limit 回まで」の素朴なスライディングウィンドウ。
    プレビューはジョブではないので日次クォータは消費しないが、連打で変換が
    走り続けないようにここで止める。キャッシュヒットは数えない(コストが無いため)。
    """

    def __init__(
        self,
        *,
        limit_env: str = RATE_LIMIT_ENV,
        window_env: str = RATE_WINDOW_ENV,
        default_limit: int = DEFAULT_RATE_LIMIT,
        default_window: float = DEFAULT_RATE_WINDOW,
    ) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._limit_env = limit_env
        self._window_env = window_env
        self._default_limit = default_limit
        self._default_window = default_window
        self._last_prune = 0.0
        self._max_keys = 10_000

    def allow(self, key: str, now: float | None = None) -> bool:
        """1回ぶん記録して、上限内なら True。超過なら記録せず False。"""
        limit = int(_env_number(self._limit_env, self._default_limit))
        window = _env_number(self._window_env, self._default_window)
        if limit <= 0 or window <= 0:
            return True
        now = time.time() if now is None else now
        cutoff = now - window
        with self._lock:
            # 全key走査は高々1分に1回。毎request O(N)にするとlimiter自体がDoS面になる。
            if now - self._last_prune >= min(window, 60.0):
                for old_key in [
                    k for k, values in self._hits.items() if not values or values[-1] <= cutoff
                ]:
                    del self._hits[old_key]
                self._last_prune = now
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    return False
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
