"""Private, time-limited diagnostics for failed public generation jobs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
import traceback
from contextvars import ContextVar
from pathlib import Path
from typing import Any

REPORT_FILENAME = "error-diagnostics.json"
MAX_RETENTION_SECONDS = 48 * 3600
CURRENT_JOB: ContextVar[str | None] = ContextVar("diagnostic_job", default=None)


def annotate_stage(exc: Exception, stage: str) -> None:
    """Keep the originating stage when parallel work propagates an exception."""
    if not getattr(exc, "_soramimic_stage", None):
        try:
            exc._soramimic_stage = stage  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass


def failure_stage(exc: Exception | None, fallback: str | None) -> str | None:
    return getattr(exc, "_soramimic_stage", fallback)


def _versions() -> dict[str, Any]:
    versions: dict[str, Any] = {"python": platform.python_version()}
    for name in ("soramimic-video", "soramimic", "soramimic-yomi", "soramimic-score"):
        try:
            package = importlib.metadata.distribution(name)
            entry = {"version": package.version}
            direct_url = json.loads(package.read_text("direct_url.json") or "{}")
            revision = direct_url.get("vcs_info", {}).get("commit_id")
            if revision:
                entry["revision"] = revision
            versions[name] = entry
        except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
            continue
    return versions


def _peak_rss_kib() -> float | None:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 if sys.platform == "darwin" else peak
    except ImportError:
        return None


def save_report(
    directory: Path,
    *,
    stage: str | None,
    error: str | None,
    exc: Exception | None,
    failed_at: float,
    expires_at: float,
    started_at: float | None,
    stages: list[dict[str, Any]],
    params: dict[str, Any],
    log: list[str],
) -> None:
    """Store details alongside inputs, outside all downloadable API resources.

    Environment variables, service configuration, cookies and credentials are
    deliberately not collected. Package provenance records include commits only.
    """
    data = {
        "schema": 1,
        "failed_at": failed_at,
        "expires_at": expires_at,
        "stage": stage,
        "error": error,
        "exception_type": type(exc).__name__ if exc else None,
        "traceback": "".join(traceback.format_exception(exc)) if exc else None,
        "versions": _versions(),
        "application_build": Path(__file__).resolve().parents[2].name,
        "elapsed_seconds": max(0, failed_at - started_at) if started_at else None,
        "process_peak_rss_kib": _peak_rss_kib(),
        "stages": stages,
        "params": params,
        "log": log,
    }
    if exc is not None:
        # CalledProcessError contains decoder/compiler output that str(exc) omits.
        for name in ("stdout", "stderr"):
            output = getattr(exc, name, None)
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if isinstance(output, str):
                data[name] = output[-64 * 1024:]
    directory.chmod(0o700)
    path = directory / REPORT_FILENAME
    temporary = directory / f".{REPORT_FILENAME}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", opener=_private_opener) as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _private_opener(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)
