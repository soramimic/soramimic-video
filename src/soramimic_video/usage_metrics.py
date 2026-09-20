"""Privacy-safe, persistent aggregate usage metrics.

The public service handles songs, lyrics, and user-provided files.  This store is
deliberately unable to retain any of them: callers may only use a fixed metric
name with low-cardinality labels, and the resulting file contains aggregate
numbers only.  It is small enough to rewrite atomically after each observation,
which keeps the counters across service restarts without adding a database or a
remote analytics dependency.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_METRIC_RE = re.compile(r"soramimic_usage_[a-z0-9_]+")
_LABEL_RE = re.compile(r"[a-z][a-z0-9_]*")
_VALUE_RE = re.compile(r"[a-z0-9_.-]{1,64}")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels_key(labels: dict[str, str]) -> str:
    return json.dumps(sorted(labels.items()), ensure_ascii=True, separators=(",", ":"))


def _labels_from_key(value: str) -> dict[str, str]:
    pairs = json.loads(value)
    if not isinstance(pairs, list):
        raise ValueError("metric labels must be a list")
    return {str(name): str(label) for name, label in pairs}


def _format_labels(labels: dict[str, str], extra: tuple[str, str] | None = None) -> str:
    values = dict(labels)
    if extra is not None:
        values[extra[0]] = extra[1]
    if not values:
        return ""
    body = ",".join(f'{name}="{_escape(value)}"' for name, value in sorted(values.items()))
    return "{" + body + "}"


class UsageMetrics:
    """Persist aggregate counters and histograms without visitor identifiers."""

    def __init__(self, path: Path, *, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self.healthy = True
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "version": 1,
            "counters": {},
            "histograms": {},
        }
        if enabled:
            self._load()

    @staticmethod
    def _validate(metric: str, labels: dict[str, str]) -> None:
        if _METRIC_RE.fullmatch(metric) is None:
            raise ValueError(f"invalid usage metric name: {metric!r}")
        for name, value in labels.items():
            if _LABEL_RE.fullmatch(name) is None:
                raise ValueError(f"invalid usage metric label: {name!r}")
            if _VALUE_RE.fullmatch(value) is None:
                raise ValueError(f"invalid usage metric value for {name}: {value!r}")

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                raw.get("version") != 1
                or not isinstance(raw.get("counters"), dict)
                or not isinstance(raw.get("histograms"), dict)
            ):
                raise ValueError("unknown usage metrics format")
            self._data = raw
        except FileNotFoundError:
            return
        except (OSError, ValueError, json.JSONDecodeError):
            # Analytics must never prevent the service from starting or serving a
            # generation request.  Disable writes to keep the unreadable file for
            # operator recovery; readiness exposes the failure.
            logger.exception("匿名利用メトリクスを読み込めません")
            self.enabled = False
            self.healthy = False

    def _save(self) -> None:
        # A rolling deploy or a test may briefly have two app instances sharing
        # StateDirectory.  Unique temporary names prevent one writer from moving
        # the other writer's in-progress file.
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._data, ensure_ascii=True, sort_keys=True),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self.healthy = True
        except OSError:
            self.healthy = False
            logger.exception("匿名利用メトリクスを保存できません")
        finally:
            temporary.unlink(missing_ok=True)

    def increment(
        self,
        metric: str,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        if not self.enabled:
            return
        dimensions = labels or {}
        self._validate(metric, dimensions)
        key = metric + "\t" + _labels_key(dimensions)
        with self._lock:
            counters: dict[str, int] = self._data["counters"]
            counters[key] = int(counters.get(key, 0)) + amount
            self._save()

    def observe(
        self,
        metric: str,
        value: float,
        bounds: tuple[float, ...],
        labels: dict[str, str] | None = None,
    ) -> None:
        if not self.enabled:
            return
        if not math.isfinite(value) or value < 0:
            return
        dimensions = labels or {}
        self._validate(metric, dimensions)
        if tuple(sorted(set(bounds))) != bounds or any(bound <= 0 for bound in bounds):
            raise ValueError("histogram bounds must be unique, increasing, and positive")
        key = metric + "\t" + _labels_key(dimensions)
        with self._lock:
            histograms: dict[str, dict[str, Any]] = self._data["histograms"]
            histogram = histograms.setdefault(
                key,
                {
                    "bounds": list(bounds),
                    "buckets": [0 for _ in bounds],
                    "count": 0,
                    "sum": 0.0,
                },
            )
            if histogram["bounds"] != list(bounds):
                raise ValueError(f"histogram bounds changed for {metric}")
            for index, bound in enumerate(bounds):
                if value <= bound:
                    histogram["buckets"][index] += 1
            histogram["count"] += 1
            histogram["sum"] += value
            self._save()

    def render_prometheus(self) -> str:
        if not self.enabled:
            return ""
        with self._lock:
            snapshot = json.loads(json.dumps(self._data))
        lines: list[str] = []
        seen_counters: set[str] = set()
        for key, value in sorted(snapshot["counters"].items()):
            metric, labels_key = key.split("\t", 1)
            labels = _labels_from_key(labels_key)
            if metric not in seen_counters:
                lines.append(f"# TYPE {metric} counter")
                seen_counters.add(metric)
            lines.append(f"{metric}{_format_labels(labels)} {int(value)}")
        seen_histograms: set[str] = set()
        for key, histogram in sorted(snapshot["histograms"].items()):
            metric, labels_key = key.split("\t", 1)
            labels = _labels_from_key(labels_key)
            if metric not in seen_histograms:
                lines.append(f"# TYPE {metric} histogram")
                seen_histograms.add(metric)
            for bound, count in zip(
                histogram["bounds"], histogram["buckets"], strict=True
            ):
                lines.append(
                    f"{metric}_bucket"
                    f'{_format_labels(labels, ("le", str(bound)))} {int(count)}'
                )
            lines.append(
                f"{metric}_bucket{_format_labels(labels, ('le', '+Inf'))} "
                f"{int(histogram['count'])}"
            )
            lines.append(f"{metric}_sum{_format_labels(labels)} {histogram['sum']:.6f}")
            lines.append(f"{metric}_count{_format_labels(labels)} {int(histogram['count'])}")
        return "".join(line + "\n" for line in lines)
