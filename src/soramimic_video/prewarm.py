"""単語リストCSVの画像を画像キャッシュへ事前ダウンロードする(prewarm-images CLI用)。

動画生成(video.build_image_cues)は初回、単語ごとにWikimedia Commonsへ画像と
クレジットを取りに行くためキャッシュが冷えていると時間がかかる。このモジュールは
単語リストCSVを読み、画像URLを直列にゆっくり(--delay)取得してキャッシュを温める。
レート制限に配慮した「事前ウォームアップ」用で、動画生成側のプリフェッチ並列化とは別。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, UnidentifiedImageError

from . import runproc
from .asset_store import MANIFEST_NAME, PENDING_MANIFEST_NAME, load_manifest
from .image_credit import (
    commons_file_title,
    fetch_commons_assets_batch,
    fetch_image_credit,
)
from .video import download_image, looks_like_svg, svg_to_png

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MAX_ASSET_BYTES = 50 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _store_lock(store: Path):
    """One synchronizer at a time; readers keep using the last atomic manifest."""
    import fcntl

    store.mkdir(parents=True, exist_ok=True)
    lock = (store / ".sync.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"別のasset syncが実行中です: {store}") from None
    try:
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _image_cached(url: str, cache_dir: Path) -> bool:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    return any(cache_dir.glob(f"{name}.*"))


def _credit_cached(url: str, cache_dir: Path) -> bool:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    return (cache_dir / "credits" / f"{name}.json").exists()


def _collect_rows(csv_paths: list[Path]) -> dict[str, dict]:
    """CSV群から http(s) の image 列を持つ行をユニークURLごとに集める(初出優先)。"""
    rows: dict[str, dict] = {}
    for path in csv_paths:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                url = (row.get("image") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                existing = rows.setdefault(url, row)
                # Repeated images are common. Preserve the first row, but do not lose
                # credit/page metadata that is only populated on a later occurrence.
                for key in ("image_page", "image_credit"):
                    if not str(existing.get(key) or "").strip() and str(row.get(key) or "").strip():
                        existing[key] = row[key]
    return rows


def wordlist_csv_paths(wordlists_dir: Path) -> list[Path]:
    return sorted(path for path in wordlists_dir.glob("*.csv") if path.is_file())


def _local_wordlist_image(url: str, wordlists_dir: Path) -> Path | None:
    parsed = urlparse(url)
    if parsed.hostname != "raw.githubusercontent.com":
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[:2] != ["soramimic", "soramimic-wordlists"]:
        return None
    relative = Path(*parts[3:])  # owner/repo/<ref>/<path>
    try:
        candidate = (wordlists_dir / relative).resolve()
        candidate.relative_to(wordlists_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _validate_image(path: Path, max_bytes: int = MAX_ASSET_BYTES) -> None:
    if path.stat().st_size <= 0 or path.stat().st_size > max_bytes:
        raise ValueError(f"画像サイズが不正です: {path.stat().st_size} bytes")
    try:
        with Image.open(path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("画像MIME/内容を認識できません") from e


def _metadata_for(url: str, cache: Path) -> dict:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    path = cache / ".metadata" / f"{name}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _store_content(source: Path, store: Path) -> tuple[str, str]:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    suffix = source.suffix.lower() if source.suffix else ".img"
    relative = Path("images") / digest[:2] / f"{digest}{suffix}"
    destination = store / relative
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, destination)
    return relative.as_posix(), digest


def _prepare_local_image(source: Path, staging: Path) -> Path:
    """Normalize a submodule SVG without a release-path-keyed cache copy."""
    data = source.read_bytes()
    if not looks_like_svg(data):
        return source
    digest = hashlib.sha256(data).hexdigest()
    destination = staging / ".local-normalized" / f"{digest}.png"
    if not destination.exists():
        png = svg_to_png(data)
        if png is None:
            raise ValueError("ローカルSVGをPNGへ変換できません")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        tmp.write_bytes(png)
        os.replace(tmp, destination)
    return destination


def _credit_record(info: dict | None) -> dict:
    if info is None:
        return {"status": "unknown", "attribution_required": None, "credit_text": ""}
    return {"status": "known", **info}


def _load_json_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _candidate_manifest(store: Path) -> dict:
    pending = _load_json_manifest(store / PENDING_MANIFEST_NAME)
    return pending or load_manifest(store)


def _manifest_health(manifest: dict, store: Path) -> dict[str, int]:
    assets = manifest.get("assets", {}) if isinstance(manifest.get("assets"), dict) else {}
    active_entries = [entry for entry in assets.values() if "orphaned_at" not in entry]
    missing = sum(
        entry.get("status") == "available"
        and (
            not isinstance(entry.get("local_path"), str)
            or not (store / entry["local_path"]).is_file()
        )
        for entry in active_entries
    )
    return {
        "active": len(active_entries),
        "available": sum(e.get("status") == "available" for e in active_entries),
        "failed": sum(e.get("status") != "available" for e in active_entries),
        "credit_unknown": sum(
            e.get("credit", {}).get("status") not in {"known", "not_applicable"}
            for e in active_entries
        ),
        "missing_files": missing,
    }


def sync_asset_store(
    csv_paths: list[Path],
    store: Path,
    *,
    wordlists_dir: Path,
    revalidate: bool = False,
    dry_run: bool = False,
    max_bytes: int = MAX_ASSET_BYTES,
    skip_revalidate_urls: set[str] | None = None,
    download_workers: int = 2,
) -> dict[str, int]:
    """Synchronize all built-in wordlist assets into an atomic persistent manifest."""
    rows = _collect_rows(csv_paths)
    skip_revalidate_urls = skip_revalidate_urls or set()
    old = _candidate_manifest(store)
    old_assets = old.get("assets", {}) if isinstance(old.get("assets"), dict) else {}
    new_count = sum(url not in old_assets for url in rows)
    changed_candidates = (
        sum(url in old_assets and url not in skip_revalidate_urls for url in rows)
        if revalidate else 0
    )
    orphaned = sum(url not in rows for url in old_assets)
    if dry_run:
        unknown = sum(
            entry.get("credit", {}).get("status") == "unknown"
            for entry in old_assets.values()
            if isinstance(entry, dict)
        )
        return {
            "total": len(rows), "new": new_count, "updated": changed_candidates,
            "unchanged": len(rows) - new_count - changed_candidates,
            "failed": 0, "credit_unknown": unknown, "orphaned": orphaned,
        }

    with _store_lock(store):
        # Re-read after acquiring the lock so no successful concurrent generation is lost.
        old = _candidate_manifest(store)
        old_assets = old.get("assets", {}) if isinstance(old.get("assets"), dict) else {}
        assets = {url: dict(entry) for url, entry in old_assets.items() if isinstance(entry, dict)}
        staging = store / ".download-cache"
        new = updated = unchanged = failed = credit_failed = 0
        commons_pending: dict[str, str] = {}
        commons_requests: dict[str, str] = {}
        for url, row in rows.items():
            previous = assets.get(url)
            skip_revalidate = url in skip_revalidate_urls
            current = previous and previous.get("status") == "available"
            needs_image = not current or (revalidate and not skip_revalidate)
            credit = previous.get("credit", {}) if previous else {}
            needs_credit = (
                revalidate and not skip_revalidate
            ) or credit.get("status") not in {"known", "not_applicable"}
            page = str(row.get("image_page") or "")
            if (needs_image or needs_credit) and commons_file_title(url, page):
                commons_requests[url] = page
        commons_assets = fetch_commons_assets_batch(
            commons_requests, cancel_check=runproc.raise_if_cancelled
        ) if commons_requests else {}
        # Network fetches are independent by URL and dominate cold-sync time. Fetch at
        # most two concurrently (four caused Commons 429s in prior measurements), then
        # validate and publish content sequentially to keep the store mutation atomic.
        workers = max(1, download_workers)
        executor = ThreadPoolExecutor(max_workers=workers)
        futures: dict[str, Future[Path | None]] = {}
        before_metadata: dict[str, dict] = {}
        try:
            for url in rows:
                previous = assets.get(url)
                skip_revalidate = url in skip_revalidate_urls
                current = previous and previous.get("status") == "available"
                if current and (not revalidate or skip_revalidate):
                    continue
                if _local_wordlist_image(url, wordlists_dir) is not None:
                    continue
                before_metadata[url] = _metadata_for(url, staging)
                futures[url] = executor.submit(
                    download_image,
                    url,
                    staging,
                    revalidate=revalidate,
                    use_asset_store=False,
                    fetch_url_override=commons_assets.get(url, {}).get("download_url"),
                )

            for index, (url, row) in enumerate(rows.items(), 1):
                runproc.raise_if_cancelled()
                previous = assets.get(url)
                skip_revalidate = url in skip_revalidate_urls
                if previous and previous.get("status") == "available" and (
                    not revalidate or skip_revalidate
                ):
                    unchanged += 1
                else:
                    try:
                        local = _local_wordlist_image(url, wordlists_dir)
                        image: Path | None
                        if local is not None:
                            image = _prepare_local_image(local, staging)
                            meta = {}
                        else:
                            image = futures[url].result()
                            meta = _metadata_for(url, staging)
                            if (
                                revalidate
                                and previous is not None
                                and meta.get("checked_at")
                                == before_metadata[url].get("checked_at")
                            ):
                                raise ValueError(
                                    "画像の更新確認に失敗しました(last-goodを維持)"
                                )
                        if image is None:
                            raise ValueError("画像を取得できません")
                        _validate_image(image, max_bytes)
                        relative, digest = _store_content(image, store)
                        entry = {
                            "source_url": url,
                            "local_path": relative,
                            "sha256": digest,
                            "etag": meta.get("etag", ""),
                            "last_modified": meta.get("last_modified", ""),
                            "checked_at": _now(),
                            "status": "available",
                        }
                        if previous and "credit" in previous:
                            entry["credit"] = previous["credit"]
                        assets[url] = entry
                        if previous:
                            updated += 1
                        else:
                            new += 1
                    except (OSError, ValueError) as e:
                        logger.warning(
                            "[%d/%d] asset取得失敗: %s (%s)",
                            index, len(rows), url, e,
                        )
                        failed += 1
                        if previous and previous.get("status") == "available":
                            previous["last_error"] = str(e)
                            previous["last_error_at"] = _now()
                        else:
                            assets[url] = {
                                "source_url": url, "status": "failed",
                                "checked_at": _now(), "last_error": str(e),
                            }

                explicit = str(row.get("image_credit") or "").strip()
                if explicit:
                    assets[url]["credit"] = {
                        "status": "known", "artist": "", "license": "",
                        "attribution_required": True, "credit_text": explicit,
                    }
                elif commons_file_title(url, str(row.get("image_page") or "")):
                    if (
                        (revalidate and not skip_revalidate)
                        or "credit" not in assets[url]
                        or assets[url]["credit"].get("status") == "unknown"
                    ):
                        commons_pending[url] = str(row.get("image_page") or "")
                elif "credit" not in assets[url]:
                    # Commons metadata is not applicable. This is distinct from a Commons
                    # lookup failure (unknown), and matches the legacy non-Commons behavior.
                    assets[url]["credit"] = {
                        "status": "not_applicable", "attribution_required": False,
                        "credit_text": "",
                    }
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if commons_pending:
            credit_results = {
                url: commons_assets.get(url, {}).get("credit")
                for url in commons_pending
            }
            for url, info in credit_results.items():
                runproc.raise_if_cancelled()
                previous_credit = assets[url].get("credit", {})
                if info is None and previous_credit.get("status") == "known":
                    previous_credit["last_error"] = "Commons metadata lookup failed"
                    previous_credit["last_error_at"] = _now()
                    credit_failed += 1
                else:
                    assets[url]["credit"] = _credit_record(info)
                    if info is None:
                        credit_failed += 1

        marked_at = _now()
        for url, entry in assets.items():
            if url not in rows:
                entry.setdefault("orphaned_at", marked_at)
            else:
                entry.pop("orphaned_at", None)
        manifest = {
            "version": MANIFEST_VERSION,
            "generated_at": _now(),
            "source": str(wordlists_dir.resolve()),
            "assets": assets,
        }
        health = _manifest_health(manifest, store)
        manifest["sync"] = {
            "image_failed": failed,
            "credit_failed": credit_failed,
            **health,
        }
        # A partial candidate is durable for the next retry, while readers continue to
        # use the last fully healthy active manifest.
        _atomic_json(store / PENDING_MANIFEST_NAME, manifest)
        promote = (
            health["active"] > 0
            and failed == 0
            and credit_failed == 0
            and health["failed"] == 0
            and health["credit_unknown"] == 0
            and health["missing_files"] == 0
        )
        if promote:
            _atomic_json(store / MANIFEST_NAME, manifest)
            (store / PENDING_MANIFEST_NAME).unlink(missing_ok=True)
    return {
        "total": len(rows), "new": new, "updated": updated, "unchanged": unchanged,
        "failed": failed,
        "credit_failed": credit_failed,
        "credit_unknown": sum(
            assets[url].get("credit", {}).get("status") == "unknown" for url in rows
        ),
        "orphaned": sum(url not in rows for url in assets),
        "promoted": int(promote),
    }


def asset_store_status(store: Path) -> dict[str, int | str]:
    manifest = load_manifest(store)
    assets = manifest.get("assets", {}) if isinstance(manifest.get("assets"), dict) else {}
    active_health = _manifest_health(manifest, store)
    pending = _load_json_manifest(store / PENDING_MANIFEST_NAME)
    pending_health = _manifest_health(pending, store) if pending else {}
    result: dict[str, int | str] = {
        "generated_at": str(manifest.get("generated_at", "")),
        "manifest_version": int(manifest.get("version", 0) or 0),
        "total": len(assets),
        "orphaned": sum("orphaned_at" in e for e in assets.values()),
        "pending": int(bool(pending)),
        **active_health,
    }
    if pending:
        result.update({f"pending_{key}": value for key, value in pending_health.items()})
        sync = pending.get("sync", {})
        result["pending_image_failed"] = int(sync.get("image_failed", 0) or 0)
        result["pending_credit_failed"] = int(sync.get("credit_failed", 0) or 0)
    return result


def prewarm_images(
    csv_paths: list[Path],
    cache_dir: Path,
    delay: float = 1.0,
    revalidate: bool = False,
) -> dict[str, int]:
    """CSVの画像URLを直列に取得して画像キャッシュを温める。取得数/スキップ数/失敗数を返す。

    キャッシュ済みURLはスキップ(待機もしない)。未キャッシュのみ download_image し、
    CSVの image_credit 列が空でクレジット未取得なら fetch_image_credit も行い、delay秒待つ。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = _collect_rows(csv_paths)
    total = len(rows)
    fetched = skipped = failed = revalidated = 0
    for i, (url, row) in enumerate(rows.items(), 1):
        was_cached = _image_cached(url, cache_dir)
        if was_cached and not revalidate:
            skipped += 1
            logger.info("[%d/%d] スキップ(キャッシュ済み): %s", i, total, url)
            continue
        action = "更新確認" if was_cached else "取得"
        logger.info("[%d/%d] %s: %s", i, total, action, url)
        raw = download_image(url, cache_dir, revalidate=revalidate)
        if raw is None:
            failed += 1
            continue
        if was_cached:
            revalidated += 1
        else:
            fetched += 1
        # image_credit 列が埋まっている行はクレジット取得不要(video側もその文言を優先)
        has_credit_col = bool(str(row.get("image_credit") or "").strip())
        if not has_credit_col and not _credit_cached(url, cache_dir):
            fetch_image_credit(url, (row.get("image_page") or "").strip(), cache_dir)
        if delay > 0:
            time.sleep(delay)
    logger.info(
        "prewarm完了: 取得 %d / 更新確認 %d / スキップ %d / 失敗 %d (URL計 %d)",
        fetched, revalidated, skipped, failed, total,
    )
    return {
        "fetched": fetched,
        "revalidated": revalidated,
        "skipped": skipped,
        "failed": failed,
        "total": total,
    }
