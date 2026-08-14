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
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urlparse,
    urlsplit,
    urlunsplit,
)

import requests
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
MAX_SOURCE_MANIFEST_BYTES = 5 * 1024 * 1024
SOURCE_MANIFEST_URL = (
    "https://github.com/soramimic/soramimic-wordlists/releases/download/"
    "release-image-source-manifest-v1/source-manifest.json"
)
SOURCE_MANIFEST_SCHEMA = "soramimic.release-image-source-manifest"
SOURCE_MANIFEST_REPOSITORY = "soramimic/soramimic-wordlists"
SOURCE_MANIFEST_JSON_SCHEMA = (
    "https://github.com/soramimic/soramimic-wordlists/blob/main/"
    "assets/release-image-source-manifest-v1.schema.json"
)
SOURCE_RELEASE_URL_PREFIX = (
    "https://github.com/soramimic/soramimic-wordlists/releases/download/"
)


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
    destination_ok = False
    if destination.exists():
        try:
            destination_ok = hashlib.sha256(destination.read_bytes()).hexdigest() == digest
        except OSError:
            pass
    if not destination_ok:
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
    # A failed candidate must never become the comparison base.  In particular, a
    # partially downloaded source-manifest revision is retried in full next time.
    return load_manifest(store)


def _iso_datetime(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"source manifestの{field}が不正です")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"source manifestの{field}が不正です") from e
    if parsed.tzinfo is None:
        raise ValueError(f"source manifestの{field}にtimezoneがありません")
    return value


def _validate_source_manifest(value: object) -> dict:
    """Validate the intentionally small, closed v1 publisher contract."""
    if not isinstance(value, dict):
        raise ValueError("source manifestはJSON objectである必要があります")
    required = {
        "$schema", "schema", "version", "revision", "generated_at", "repository",
        "assets",
    }
    if set(value) != required:
        raise ValueError("source manifestのtop-level fieldがv1 schemaと一致しません")
    if value["$schema"] != SOURCE_MANIFEST_JSON_SCHEMA:
        raise ValueError("source manifestの$schemaが不正です")
    if value["schema"] != SOURCE_MANIFEST_SCHEMA or value["version"] != 1:
        raise ValueError("未対応のsource manifest schema/versionです")
    if value["repository"] != SOURCE_MANIFEST_REPOSITORY:
        raise ValueError("source manifestのrepositoryが不正です")
    if not isinstance(value["revision"], int) or isinstance(value["revision"], bool) \
            or value["revision"] < 1:
        raise ValueError("source manifestのrevisionが不正です")
    _iso_datetime(value["generated_at"], "generated_at")
    if not isinstance(value["assets"], dict):
        raise ValueError("source manifestのassetsが不正です")
    allowed_asset = {"revision", "updated_at", "sha256", "size", "note"}
    required_asset = {"revision", "updated_at", "sha256", "size"}
    for url, entry in value["assets"].items():
        if not isinstance(url, str) or not url.startswith(SOURCE_RELEASE_URL_PREFIX):
            raise ValueError(f"source manifestのcanonical URLが不正です: {url!r}")
        if not isinstance(entry, dict) or not required_asset <= set(entry) \
                or not set(entry) <= allowed_asset:
            raise ValueError(f"source manifest entryが不正です: {url}")
        revision = entry["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError(f"source revisionが不正です: {url}")
        _iso_datetime(entry["updated_at"], f"updated_at ({url})")
        digest = entry["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 \
                or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError(f"source sha256が不正です: {url}")
        size = entry["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"source sizeが不正です: {url}")
        if "note" in entry and not isinstance(entry["note"], str):
            raise ValueError(f"source noteが不正です: {url}")
    return value


def _cache_busted_url(url: str, **values: object) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items()})
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _bounded_response_bytes(response: requests.Response, max_bytes: int) -> bytes:
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > max_bytes:
                raise ValueError(f"response sizeが上限を超えています: {length} bytes")
        except ValueError as e:
            if "上限" in str(e):
                raise
            raise ValueError("Content-Lengthが不正です") from e
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"response sizeが上限を超えています: >{max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_source_manifest(url: str = SOURCE_MANIFEST_URL) -> tuple[dict, str]:
    try:
        response = requests.get(
            _cache_busted_url(url, sync_nonce=time.time_ns()),
            headers={"Accept": "application/json", "Cache-Control": "no-cache"},
            timeout=30, stream=True,
        )
        response.raise_for_status()
        raw = _bounded_response_bytes(response, MAX_SOURCE_MANIFEST_BYTES)
        value = json.loads(raw)
    except (requests.RequestException, ValueError) as e:
        raise ValueError(f"source manifestを取得できません: {e}") from e
    return _validate_source_manifest(value), hashlib.sha256(raw).hexdigest()


def _fetch_verified_source_image(
    url: str, source: dict, staging: Path, max_bytes: int,
) -> tuple[Path, str]:
    expected_size = source["size"]
    if expected_size > max_bytes:
        raise ValueError(f"source sizeが上限を超えています: {expected_size} bytes")
    try:
        response = requests.get(
            _cache_busted_url(
                url, source_revision=source["revision"], source_sha256=source["sha256"],
            ),
            headers={"Cache-Control": "no-cache"}, timeout=30, stream=True,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(f"Release assetを取得できません: {e}") from e
    raw = _bounded_response_bytes(response, min(max_bytes, expected_size + 1))
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != expected_size:
        raise ValueError(f"source size不一致: expected={expected_size} actual={len(raw)}")
    if digest != source["sha256"]:
        raise ValueError(f"source sha256不一致: expected={source['sha256']} actual={digest}")
    suffix = Path(urlparse(url).path).suffix.lower() or ".img"
    destination = staging / ".source-raw" / f"{digest}{suffix}"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, destination)
    return destination, digest


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


def _entry_available(entry: dict | None, store: Path) -> bool:
    if not entry or entry.get("status") != "available":
        return False
    relative = entry.get("local_path")
    if not isinstance(relative, str):
        return False
    try:
        path = (store / relative).resolve()
        path.relative_to(store.resolve())
    except (OSError, ValueError):
        return False
    return path.is_file()


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
    mode: str = "manifest",
    source_manifest_url: str = SOURCE_MANIFEST_URL,
) -> dict[str, int]:
    """Synchronize all built-in wordlist assets into an atomic persistent manifest."""
    if mode not in {"manifest", "full"}:
        raise ValueError(f"不正なasset sync modeです: {mode}")
    if revalidate:
        mode = "full"
    rows = _collect_rows(csv_paths)
    skip_revalidate_urls = skip_revalidate_urls or set()
    source_manifest: dict | None = None
    source_manifest_sha256 = ""
    source_assets: dict[str, dict] = {}
    if any(url.startswith(SOURCE_RELEASE_URL_PREFIX) for url in rows):
        source_manifest, source_manifest_sha256 = fetch_source_manifest(source_manifest_url)
        source_assets = source_manifest["assets"]
    checked_at = _now()
    old = _candidate_manifest(store)
    old_assets = old.get("assets", {}) if isinstance(old.get("assets"), dict) else {}
    old_source = old.get("source_manifest", {})
    if source_manifest is not None and isinstance(old_source, dict) \
            and old_source.get("revision") is not None:
        if source_manifest["revision"] < old_source["revision"]:
            raise ValueError("source manifest revisionがactiveより古いため拒否しました")
        if source_manifest["revision"] == old_source["revision"] \
                and old_source.get("sha256") not in {None, source_manifest_sha256}:
            raise ValueError("同じsource manifest revisionで内容が変化しています")
    for url, declared in source_assets.items():
        previous = old_assets.get(url, {})
        previous_revision = previous.get("source_revision") if isinstance(previous, dict) else None
        previous_hash = previous.get("source_sha256") if isinstance(previous, dict) else None
        if isinstance(previous_revision, int):
            if declared["revision"] < previous_revision:
                raise ValueError(f"source revisionがactiveより古いため拒否しました: {url}")
            if declared["revision"] == previous_revision \
                    and isinstance(previous_hash, str) and previous_hash != declared["sha256"]:
                raise ValueError(f"同じsource revisionでsha256が変化しています: {url}")
    new_count = sum(url not in old_assets for url in rows)
    changed_candidates = sum(
        url in old_assets
        and url not in skip_revalidate_urls
        and (
            mode == "full"
            or (
                url in source_assets
                and old_assets[url].get("source_sha256") != source_assets[url]["sha256"]
            )
        )
        for url in rows
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
            current = _entry_available(previous, store)
            source_entry = source_assets.get(url)
            source_changed = bool(
                source_entry
                and (not previous or previous.get("source_sha256") != source_entry["sha256"])
            )
            needs_image = not current or (
                not skip_revalidate and (mode == "full" or source_changed)
            )
            credit = previous.get("credit", {}) if previous else {}
            needs_credit = (
                mode == "full" and not skip_revalidate
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
        source_futures: dict[str, Future[tuple[Path, str]]] = {}
        before_metadata: dict[str, dict] = {}
        try:
            for url in rows:
                previous = assets.get(url)
                skip_revalidate = url in skip_revalidate_urls
                current = _entry_available(previous, store)
                source_entry = source_assets.get(url)
                source_changed = bool(
                    source_entry
                    and (not previous or previous.get("source_sha256") != source_entry["sha256"])
                )
                if current and (skip_revalidate or (mode != "full" and not source_changed)):
                    continue
                if _local_wordlist_image(url, wordlists_dir) is not None:
                    continue
                if source_entry is not None:
                    source_futures[url] = executor.submit(
                        _fetch_verified_source_image, url, source_entry, staging, max_bytes,
                    )
                    continue
                before_metadata[url] = _metadata_for(url, staging)
                futures[url] = executor.submit(
                    download_image,
                    url,
                    staging,
                    revalidate=mode == "full",
                    use_asset_store=False,
                    fetch_url_override=commons_assets.get(url, {}).get("download_url"),
                )

            for index, (url, row) in enumerate(rows.items(), 1):
                runproc.raise_if_cancelled()
                previous = assets.get(url)
                skip_revalidate = url in skip_revalidate_urls
                source_entry = source_assets.get(url)
                source_changed = bool(
                    source_entry
                    and (not previous or previous.get("source_sha256") != source_entry["sha256"])
                )
                if _entry_available(previous, store) and (
                    skip_revalidate or (mode != "full" and not source_changed)
                ):
                    assert previous is not None
                    if source_entry is not None:
                        previous.update({
                            "source_revision": source_entry["revision"],
                            "source_sha256": source_entry["sha256"],
                            "source_size": source_entry["size"],
                            "source_updated_at": source_entry["updated_at"],
                            "source_checked_at": checked_at,
                        })
                        if "note" in source_entry:
                            previous["source_note"] = source_entry["note"]
                        else:
                            previous.pop("source_note", None)
                    unchanged += 1
                else:
                    try:
                        local = _local_wordlist_image(url, wordlists_dir)
                        image: Path | None
                        meta: dict
                        if local is not None:
                            image = _prepare_local_image(local, staging)
                            meta = {}
                        elif source_entry is not None:
                            raw_image, _ = source_futures[url].result()
                            image = _prepare_local_image(raw_image, staging)
                            meta = {}
                        else:
                            image = futures[url].result()
                            meta = _metadata_for(url, staging)
                            if (
                                mode == "full"
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
                        if source_entry is not None:
                            entry.update({
                                "source_revision": source_entry["revision"],
                                "source_sha256": source_entry["sha256"],
                                "source_size": source_entry["size"],
                                "source_updated_at": source_entry["updated_at"],
                                "source_checked_at": checked_at,
                                "blob_sha256": digest,
                            })
                            if "note" in source_entry:
                                entry["source_note"] = source_entry["note"]
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
                        (mode == "full" and not skip_revalidate)
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
        if source_manifest is not None:
            manifest["source_manifest"] = {
                "url": source_manifest_url,
                "schema": source_manifest["schema"],
                "version": source_manifest["version"],
                "revision": source_manifest["revision"],
                "generated_at": source_manifest["generated_at"],
                "sha256": source_manifest_sha256,
                "checked_at": checked_at,
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
