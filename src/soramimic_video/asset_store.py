"""Read-only access to the prewarmed, release-independent image asset store."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ASSET_STORE_ENV = "SORAMIMIC_VIDEO_ASSET_STORE"
MANIFEST_NAME = "manifest.json"
PENDING_MANIFEST_NAME = "manifest.pending.json"


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
    return entry if isinstance(entry, dict) else None


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


def local_credit(url: str, store: Path | None = None) -> tuple[bool, dict | None]:
    """Return (managed, credit), preserving known-no-attribution vs unknown."""
    entry = manifest_entry(url, store or configured_asset_store())
    if entry is None:
        return False, None
    credit = entry.get("credit")
    if not isinstance(credit, dict) or credit.get("status") == "unknown":
        return True, None
    return True, credit
