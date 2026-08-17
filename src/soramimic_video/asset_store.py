"""Read-only access to the prewarmed, release-independent image asset store."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from functools import lru_cache
from pathlib import Path

ASSET_STORE_ENV = "SORAMIMIC_VIDEO_ASSET_STORE"
MANIFEST_NAME = "manifest.json"
PENDING_MANIFEST_NAME = "manifest.pending.json"
MAX_PREVIEW_SOURCE_BYTES = 32 * 1024 * 1024


def configured_asset_store() -> Path | None:
    value = os.environ.get(ASSET_STORE_ENV, "").strip()
    return Path(value) if value else None


@lru_cache(maxsize=2)
def _read_manifest(path: str, mtime_ns: int) -> dict:  # noqa: ARG001 - mtime is cache key
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_manifest(store: Path) -> dict:
    path = store / MANIFEST_NAME
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    return _read_manifest(str(path), mtime)


def manifest_entry(url: str, store: Path | None = None) -> dict | None:
    root = store or configured_asset_store()
    if root is None:
        return None
    entry = load_manifest(root).get("assets", {}).get(url)
    # Entries retained only as synchronization history are outside the active
    # manifest scope and must keep using the ordinary runtime fallback.
    if not isinstance(entry, dict) or "orphaned_at" in entry:
        return None
    return entry


def local_asset(url: str, store: Path | None = None) -> tuple[bool, Path | None]:
    """Return (managed, local path). A managed failed entry must not hit the network."""
    root = store or configured_asset_store()
    if root is None:
        return False, None
    entry = manifest_entry(url, root)
    if entry is None:
        return False, None
    relative = entry.get("local_path")
    if entry.get("status") != "available" or not isinstance(relative, str):
        return True, None
    try:
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return True, None
    return True, path if path.is_file() else None


def verified_preview_asset(
    url: str,
    store: Path | None = None,
    *,
    max_bytes: int = MAX_PREVIEW_SOURCE_BYTES,
) -> tuple[bool, Path | None, str, str]:
    """HTTP preview用にmanifestとblobを厳格検証する。

    ``(managed, path, revision, sha256)`` を返す。manifest管理下のentryが壊れて
    いる場合は ``managed=True, path=None`` とし、runtime cacheやnetworkへ
    フォールバックさせない。動画生成向けの :func:`local_asset` は従来どおり
    原本pathを返すため、この検証はHTTP派生画像の境界だけで使う。
    """
    root = store or configured_asset_store()
    if root is None:
        return False, None, "", ""
    entry = load_manifest(root).get("assets", {}).get(url)
    if not isinstance(entry, dict):
        # previewでは設定済みstoreをauthoritativeに扱い、manifest外assetを
        # runtime cache/networkへ逃がさない。
        return True, None, "", ""
    relative = entry.get("local_path")
    digest = entry.get("blob_sha256") or entry.get("sha256")
    revision = entry.get("source_revision", entry.get("revision", ""))
    if (
        entry.get("status") != "available"
        or "orphaned_at" in entry
        or not isinstance(relative, str)
        or not relative
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        return True, None, str(revision), str(digest or "")
    rel = Path(relative)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return True, None, str(revision), digest
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root / rel
        current = resolved_root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                return True, None, str(revision), digest
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not resolved.is_file() or resolved.stat().st_size > max_bytes:
            return True, None, str(revision), digest
        hasher = hashlib.sha256()
        with resolved.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        actual = hasher.hexdigest()
    except (OSError, ValueError):
        return True, None, str(revision), digest
    if not hmac.compare_digest(actual, digest.lower()):
        return True, None, str(revision), digest
    return True, resolved, str(revision), actual


def local_credit(url: str, store: Path | None = None) -> tuple[bool, dict | None]:
    """Return (managed, credit), preserving known-no-attribution vs unknown."""
    entry = manifest_entry(url, store or configured_asset_store())
    if entry is None:
        return False, None
    credit = entry.get("credit")
    if not isinstance(credit, dict) or credit.get("status") == "unknown":
        return True, None
    return True, credit
