"""Video側で持つ単語リストの表示・公開メタデータ。

単語リスト本体と絞り込み設定は submodule が正本だが、Video固有の表示名、
既定レイアウト、Simple UIでの公開可否はこのカタログを唯一の正本にする。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WORDLIST_CATALOG_PATH = Path(__file__).resolve().parent / "wordlist_catalog.json"
GUIDELINE_LABELS = {
    "https://hololivepro.com/terms/": "ホロライブプロダクション二次創作ガイドライン",
    "https://www.anycolor.co.jp/guidelines/": "ANYCOLOR二次創作ガイドライン",
    "https://vhs-city.com/aogirihighschool/guidelines/fanfic": (
        "あおぎり高校二次創作ガイドライン"
    ),
}


def load_wordlist_catalog(path: Path = WORDLIST_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    """単語リストカタログを読む。壊れている場合は空のカタログとして扱う。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): entry
        for name, entry in raw.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


def default_launch_wordlists(path: Path = WORDLIST_CATALOG_PATH) -> list[str]:
    """Simple UIで公開する単語リストをカタログの記載順で返す。"""
    return [
        name
        for name, entry in load_wordlist_catalog(path).items()
        if entry.get("launch") is True
    ]


def _guideline_label(url: str) -> str:
    if url in GUIDELINE_LABELS:
        return GUIDELINE_LABELS[url]
    hostname = urlsplit(url).hostname or url
    return f"{hostname} 二次創作ガイドライン"


def _safe_terms_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _csv_terms_pages(csv_path: Path, usage: str) -> list[str]:
    """Return distinct terms URLs for restricted rows, preserving CSV order."""
    try:
        with csv_path.open(encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle)
            urls = [
                _safe_terms_url(row.get("image_terms_page"))
                for row in rows
                if row.get("image_usage") == usage
            ]
    except (OSError, UnicodeError, csv.Error):
        return []
    return list(dict.fromkeys(url for url in urls if url))


def load_wordlist_image_policies(
    wordlists_dir: Path,
    catalog_path: Path = WORDLIST_CATALOG_PATH,
) -> dict[str, dict[str, Any]]:
    """Return UI image policies with all terms URLs found in each packaged CSV."""
    policies: dict[str, dict[str, Any]] = {}
    for name, entry in load_wordlist_catalog(catalog_path).items():
        source = entry.get("image_policy")
        if not isinstance(source, dict):
            continue
        policy = dict(source)
        usage = str(policy.get("usage") or "")
        urls = _csv_terms_pages(wordlists_dir / f"{name}.csv", usage) if usage else []
        if not urls:
            fallback = _safe_terms_url(policy.get("terms"))
            urls = [fallback] if fallback else []
        policy["terms_pages"] = [
            {"url": url, "label": _guideline_label(url)} for url in urls
        ]
        policies[name] = policy
    return policies
