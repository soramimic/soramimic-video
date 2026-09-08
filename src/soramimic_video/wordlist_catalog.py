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
    "https://firststage-pro.com/guideline/": (
        "FIRST STAGE PRODUCTION（いちプロ）二次創作ガイドライン"
    ),
    "https://hololivepro.com/terms/": "ホロライブプロダクション二次創作ガイドライン",
    "https://www.anycolor.co.jp/guidelines/": "ANYCOLOR二次創作ガイドライン",
    "https://realize-pro.com/guideline/": "りあぷろ二次創作ガイドライン",
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
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _csv_terms_pages(csv_path: Path, usage: str) -> list[dict[str, Any]]:
    """Group exact URLs with their organizations and people in CSV order."""
    groups: dict[str, dict[str, Any]] = {}
    try:
        with csv_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("image_usage") != usage:
                    continue
                url = _safe_terms_url(row.get("image_terms_page"))
                if not url:
                    continue
                group = groups.setdefault(url, {"url": url, "label": _guideline_label(url)})
                for field, value in (("people", row.get("original") or row.get("surface")),
                                     ("organizations", row.get("org"))):
                    value = str(value or "").strip()
                    if value and value != "NA":
                        values = group.setdefault(field, [])
                        if value not in values:
                            values.append(value)
    except (OSError, UnicodeError, csv.Error):
        return []
    for group in groups.values():
        if group["url"] not in GUIDELINE_LABELS:
            names = list(group.get("organizations", []))
            if len(group.get("people", [])) == 1 or not names:
                names.extend(group.get("people", []))
            if names:
                group["label"] = " / ".join(names) + " 利用ガイドライン"
    return list(groups.values())


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
        terms = _csv_terms_pages(wordlists_dir / f"{name}.csv", usage) if usage else []
        if not terms:
            fallback = _safe_terms_url(policy.get("terms"))
            terms = [{"url": fallback, "label": _guideline_label(fallback)}] if fallback else []
        policy["terms_pages"] = terms
        policies[name] = policy
    return policies
