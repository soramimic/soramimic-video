"""Wikimedia Commons画像のクレジット(作者・ライセンス)取得。

動画フレームに焼き込む「クレジット表記」の文言を、Commons APIのextmetadata
(Artist / LicenseShortName / AttributionRequired)から作る。パブリックドメイン
やCC0など表記不要(AttributionRequired=false)の画像では credit_text を空にし、
呼び出し側は表記が必要な画像だけを表示できる。

取得結果は画像URLごとに <画像キャッシュ>/credits/*.json にキャッシュする
(取得失敗はキャッシュしない=次回再試行)。Commons以外のURLは対象外で、
その場合は単語リストCSVの image_credit 列で文言を直接与えられる(video.py参照)。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import unquote

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "soramimic-video/0.1 (https://github.com/soramimic/soramimic-video)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_MAX_ARTIST_LEN = 40  # Artistは長いHTML(表など)のことがあるので焼き込み用に切り詰める

_RETRY_STATUS = {429, 503}  # レート制限・一時的な過負荷は再試行の価値がある
_MAX_RETRY_AFTER = 30.0  # Retry-Afterが極端に長くても待つのはここまで


def _retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    """429/503応答のRetry-Afterヘッダ(秒数形式)を尊重する。上限は _MAX_RETRY_AFTER。

    HTTP-date形式のRetry-Afterは解釈せずバックオフのfallbackにフォールバックする。
    """
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return min(float(value), _MAX_RETRY_AFTER)
        except ValueError:
            pass
    return fallback


def http_get_with_retry(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float = 30,
    max_attempts: int = 3,
) -> requests.Response:
    """requests.getに429/503リトライ+指数バックオフを足した共通ヘルパ。

    - 最大 max_attempts 回試行。429/503応答はRetry-Afterヘッダ(あれば、上限30秒)か
      指数バックオフ(1秒→4秒)を待って再試行する
    - 接続エラー/タイムアウトも同じバックオフで再試行する
    - リトライを使い切ったら requests.RequestException(HTTPError含む)を送出するので、
      既存の「失敗したらwarningログを出してNoneを返す」呼び出し側はそのまま機能する
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        last = attempt == max_attempts
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if last:
                raise
            logger.warning(
                "接続に失敗、%.1f秒後に再試行 (%d/%d): %s (%s)",
                delay, attempt, max_attempts, url, e,
            )
        else:
            if resp.status_code in _RETRY_STATUS and not last:
                wait = _retry_after_seconds(resp, delay)
                logger.warning(
                    "HTTP %d、%.1f秒後に再試行 (%d/%d): %s",
                    resp.status_code, wait, attempt, max_attempts, url,
                )
                time.sleep(wait)
                delay *= 4
                continue
            resp.raise_for_status()  # 最終試行の429/503もここでHTTPErrorになる
            return resp
        time.sleep(delay)  # 接続エラーのバックオフ
        delay *= 4
    raise AssertionError("unreachable")  # ループは必ずreturnかraiseで抜ける


def commons_file_title(image_url: str, image_page: str = "") -> str | None:
    """CommonsのURLから APIに渡す "File:..." タイトルを取り出す。

    image(Special:FilePath/<名前>)と image_page(/wiki/File:<名前>)のどちらの
    形式も受け付ける。Commons以外のURL(ローカルパス・他サイト)は None。
    """
    for url in (image_page, image_url):
        if not url or "commons.wikimedia.org" not in url:
            continue
        for marker in ("Special:FilePath/", "/wiki/File:", "/File:"):
            if marker in url:
                name = url.split(marker, 1)[1].split("?", 1)[0]
                name = unquote(name).strip()
                if name:
                    return f"File:{name}"
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", text))).strip()


def credit_from_extmetadata(meta: dict) -> dict:
    """extmetadataからクレジット情報を作る。表記不要なら credit_text は空。"""
    artist = _strip_html(str(meta.get("Artist", {}).get("value", "")))
    if len(artist) > _MAX_ARTIST_LEN:
        artist = artist[: _MAX_ARTIST_LEN - 1] + "…"
    license_name = _strip_html(str(meta.get("LicenseShortName", {}).get("value", "")))
    required = str(meta.get("AttributionRequired", {}).get("value", "true")).lower() != "false"
    if required:
        parts = [p for p in (artist, license_name) if p]
        credit_text = ", ".join([*parts, "via Wikimedia Commons"])
    else:
        credit_text = ""
    return {
        "artist": artist,
        "license": license_name,
        "attribution_required": required,
        "credit_text": credit_text,
    }


def fetch_image_credit(
    image_url: str, image_page: str, cache_dir: Path, cached_only: bool = False
) -> dict | None:
    """Commons画像のクレジット情報を取得する(URLごとにキャッシュ)。

    Commons以外のURLや取得失敗は None(呼び出し側は表記なしで続行)。
    cached_only=True なら通信せず、キャッシュ済みのぶんだけ返す
    (待てない用途=サムネのプレビュー生成向け)。
    """
    title = commons_file_title(image_url, image_page)
    if title is None:
        return None
    cache = cache_dir / "credits" / f"{hashlib.sha1(image_url.encode()).hexdigest()[:16]}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if cached_only:
        return None
    try:
        resp = http_get_with_retry(
            COMMONS_API,
            params={
                "action": "query",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "extmetadata",
                "format": "json",
                "formatversion": "2",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        pages = resp.json()["query"]["pages"]
        meta = pages[0].get("imageinfo", [{}])[0].get("extmetadata", {})
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logger.warning("画像クレジットの取得に失敗: %s (%s)", title, e)
        return None
    info = credit_from_extmetadata(meta)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info
