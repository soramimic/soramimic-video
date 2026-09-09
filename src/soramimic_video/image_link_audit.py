"""Check live image endpoints without changing image caches or word lists."""

from __future__ import annotations

import csv
import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import requests

from .asset_store import is_builtin_asset_url
from .image_credit import USER_AGENT
from .image_usage import NONCOMMERCIAL_FANWORK
from .prewarm import _atomic_json, _store_lock, wordlist_csv_paths

logger = logging.getLogger(__name__)


def collect_links(wordlists: Path, scope: str) -> dict[str, list[dict[str, str]]]:
    """Retain every affected name, deduplicating requests by URL."""
    if scope not in {"all", "external-fanwork"}:
        raise ValueError(f"不正な検査範囲: {scope}")
    paths = wordlist_csv_paths(wordlists)
    if not paths:
        raise ValueError(f"単語リストCSVがありません: {wordlists}")
    links: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                url = (row.get("image") or "").strip()
                if not url.startswith(("https://", "http://")):
                    continue
                if scope == "external-fanwork" and (
                    (row.get("image_usage") or "").strip() != NONCOMMERCIAL_FANWORK
                    or is_builtin_asset_url(url)
                ):
                    continue
                reference = {
                    "wordlist": path.stem,
                    "name": row.get("original") or row.get("surface") or "",
                }
                refs = links.setdefault(url, [])
                if reference not in refs:
                    refs.append(reference)
    if not links:
        raise ValueError("検査対象の画像URLがありません")
    return links


def _image_prefix(prefix: bytes) -> bool:
    """Recognize image headers when a download uses application/octet-stream."""
    if prefix.startswith((
        b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
        b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00",
    )):
        return True
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return True
    if prefix[4:8] == b"ftyp" and prefix[8:12] in {b"avif", b"avis"}:
        return True
    text = prefix.lstrip().lower()
    return text.startswith((b"<svg", b"<?xml")) and b"<svg" in text


def probe(url: str, timeout: float) -> dict:
    """Stream only a small prefix; 403/429/timeouts are not confirmed broken links."""
    try:
        with requests.get(
            url, headers={"User-Agent": USER_AGENT}, stream=True,
            timeout=(timeout, timeout), allow_redirects=True,
        ) as response:
            code = response.status_code
            result = {"http_status": code, "final_url": response.url}
            if code in {404, 410}:
                return {**result, "status": "broken", "reason": f"HTTP {code}"}
            if code != 200:
                return {**result, "status": "unavailable", "reason": f"HTTP {code}"}
            prefix = next(response.iter_content(chunk_size=1024), b"")
            text_prefix = prefix.lstrip().lower()
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if not text_prefix:
                status, reason = "invalid", "empty response"
            elif content_type in {"text/html", "application/xhtml+xml"} or text_prefix.startswith(
                (b"<!doctype html", b"<html")
            ):
                status, reason = "invalid", "HTML instead of image"
            elif content_type.startswith("image/") or _image_prefix(prefix):
                status, reason = "ok", "image endpoint reachable"
            else:
                status, reason = "unavailable", f"unrecognized content type: {content_type}"
            return {**result, "status": status, "reason": reason}
    except requests.RequestException as exc:
        return {"status": "unavailable", "http_status": None, "reason": type(exc).__name__}


def audit_links(
    wordlists: Path, report_dir: Path, *, scope: str = "all",
    max_urls: int = 0, workers: int = 2, timeout: float = 15, delay: float = 0.5,
) -> dict:
    if workers not in {1, 2} or not 0 < timeout <= 60 or not 0 <= delay <= 60:
        raise ValueError("workers=1..2、timeout=0..60(0を除く)、delay=0..60が必要です")
    if max_urls < 0:
        raise ValueError("max_urlsは0以上が必要です")
    links = collect_links(wordlists, scope)
    with _store_lock(report_dir):
        latest = report_dir / "latest.json"
        previous = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
        old = {row["url"]: row for row in previous.get("results", [])}
        run_number = previous.get("run_number", 0) + 1
        # Rotate by persisted run order, even when a scheduled day is missed or
        # the wall clock moves backwards. Newly added URLs are checked first.
        selected = sorted(links, key=lambda url: (
            old.get(url, {}).get("last_checked_run", 0),
            old.get(url, {}).get("checked_at", ""), url,
        ))
        if max_urls:
            selected = selected[:max_urls]
        started = datetime.now(UTC).isoformat()

        def check(url: str) -> dict:
            result = probe(url, timeout)
            # Retry a finding once in the same run before reporting it.
            if result["status"] != "ok":
                time.sleep(max(delay, 1))
                result = probe(url, timeout)
            time.sleep(delay)
            prior = old.get(url, {})
            failures = 0 if result["status"] == "ok" else prior.get("consecutive_failures", 0) + 1
            return {
                **result, "url": url, "references": links[url],
                "checked_at": datetime.now(UTC).isoformat(), "consecutive_failures": failures,
                "last_checked_run": run_number,
            }

        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(check, selected), 1):
                results.append(result)
                if index % 100 == 0:
                    logger.info("画像URL検査: %d / %d", index, len(selected))
        counts = {status: 0 for status in ("ok", "broken", "invalid", "unavailable")}
        counts.update(Counter(row["status"] for row in results))
        current = {
            url: {**row, "references": links[url]}
            for url, row in old.items() if url in links
        }
        current.update({row["url"]: row for row in results})
        list_coverage: dict[str, dict[str, int]] = {}
        for url, references in links.items():
            for name in {ref["wordlist"] for ref in references}:
                item = list_coverage.setdefault(name, {"total_urls": 0, "checked_urls": 0})
                item["total_urls"] += 1
                item["checked_urls"] += url in current
        report = {
            "version": 2, "run_number": run_number, "scope": scope, "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(), "total": len(results),
            "counts": counts, "checked_urls": selected,
            "coverage": {
                "total_urls": len(links), "checked_urls": len(current),
                "unchecked_urls": len(links) - len(current),
                "oldest_checked_at": min(row["checked_at"] for row in current.values()),
                "wordlists": list_coverage,
            },
            "known_findings": sum(row["status"] != "ok" for row in current.values()),
            "results": [current[url] for url in sorted(current)],
        }
        # A failed/interrupted run leaves the previous completed report in place.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        # Keep the aggregate as the atomic resume point. History files contain
        # only this run's results so bounded runs also bound archive growth.
        _atomic_json(report_dir / f"{stamp}.json", {**report, "results": results})
        _atomic_json(latest, report)
        for expired in sorted(report_dir.glob("[0-9]*.json"))[:-30]:
            expired.unlink()
        logger.info("画像URL検査完了: %s (%s)", counts, latest)
        return report
