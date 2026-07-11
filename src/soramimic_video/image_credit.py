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
from pathlib import Path
from urllib.parse import unquote

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "soramimic-video/0.1 (https://github.com/soramimic/soramimic-video)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_MAX_ARTIST_LEN = 40  # Artistは長いHTML(表など)のことがあるので焼き込み用に切り詰める


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


def fetch_image_credit(image_url: str, image_page: str, cache_dir: Path) -> dict | None:
    """Commons画像のクレジット情報を取得する(URLごとにキャッシュ)。

    Commons以外のURLや取得失敗は None(呼び出し側は表記なしで続行)。
    """
    title = commons_file_title(image_url, image_page)
    if title is None:
        return None
    cache = cache_dir / "credits" / f"{hashlib.sha1(image_url.encode()).hexdigest()[:16]}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        resp = requests.get(
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
        resp.raise_for_status()
        pages = resp.json()["query"]["pages"]
        meta = pages[0].get("imageinfo", [{}])[0].get("extmetadata", {})
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logger.warning("画像クレジットの取得に失敗: %s (%s)", title, e)
        return None
    info = credit_from_extmetadata(meta)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info
