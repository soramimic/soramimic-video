"""Validated registry for wordlists that live outside every release checkout."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PRIVATE_WORDLIST_MANIFEST_ENV = "SORAMIMIC_VIDEO_PRIVATE_WORDLIST_MANIFEST"
PUBLIC_ENV = "SORAMIMIC_PUBLIC"
NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _public() -> bool:
    return os.environ.get(PUBLIC_ENV, "").strip().lower() not in (
        "", "0", "false", "no",
    )


@lru_cache(maxsize=2)
def _read(path: str, mtime_ns: int) -> dict[str, dict[str, Any]]:  # noqa: ARG001
    manifest = Path(path)
    if manifest.is_symlink() or not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    records = data.get("wordlists")
    if data.get("version") != 1 or not isinstance(records, list):
        return {}
    root = manifest.parent.resolve()
    result: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            return {}
        name = str(raw.get("name") or "")
        csv_name = str(raw.get("csv") or "")
        label = str(raw.get("label") or "").strip()
        phrase = str(raw.get("phrase") or "").strip()
        layout = str(raw.get("layout") or "").strip()
        if (
            not NAME.fullmatch(name)
            or not label
            or not phrase
            or not NAME.fullmatch(layout)
            or Path(csv_name).name != csv_name
            or not csv_name.endswith(".csv")
            or name in result
        ):
            return {}
        candidate = manifest.parent / csv_name
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return {}
        if candidate.is_symlink() or not resolved.is_file():
            return {}
        result[name] = {
            "name": name,
            "label": label,
            "phrase": phrase,
            "layout": layout,
            "csv": resolved,
        }
    return result


def entries() -> dict[str, dict[str, Any]]:
    """Return nothing in public mode, even when its environment leaks."""
    if _public():
        return {}
    value = os.environ.get(PRIVATE_WORDLIST_MANIFEST_ENV, "").strip()
    if not value:
        return {}
    path = Path(value).expanduser()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    return _read(str(path), mtime)


def resolve(name: str) -> Path | None:
    entry = entries().get(name)
    path = entry.get("csv") if entry else None
    return path if isinstance(path, Path) else None


def editor_entries() -> list[dict[str, str]]:
    return [
        {
            "text": str(entry["label"]),
            "value": name.upper(),
            "filepath": f"wordlists/{name}.csv",
            "dbtype": "tidy",
        }
        for name, entry in entries().items()
    ]
