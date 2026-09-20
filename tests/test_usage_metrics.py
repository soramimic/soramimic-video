from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from soramimic_video.usage_metrics import UsageMetrics


def test_persistent_counter_and_histogram_render(tmp_path):
    path = tmp_path / "usage-metrics.json"
    metrics = UsageMetrics(path, enabled=True)
    metrics.increment(
        "soramimic_usage_jobs_submitted_total",
        {"input_kind": "audio", "source": "upload"},
    )
    metrics.observe(
        "soramimic_usage_job_duration_seconds",
        12.5,
        (5.0, 15.0, 30.0),
        {"outcome": "done"},
    )

    restarted = UsageMetrics(path, enabled=True)
    text = restarted.render_prometheus()
    assert (
        'soramimic_usage_jobs_submitted_total{input_kind="audio",source="upload"} 1'
        in text
    )
    assert 'soramimic_usage_job_duration_seconds_bucket{le="5.0",outcome="done"} 0' in text
    assert 'soramimic_usage_job_duration_seconds_bucket{le="15.0",outcome="done"} 1' in text
    assert 'soramimic_usage_job_duration_seconds_bucket{le="+Inf",outcome="done"} 1' in text
    assert 'soramimic_usage_job_duration_seconds_sum{outcome="done"} 12.500000' in text
    assert 'soramimic_usage_job_duration_seconds_count{outcome="done"} 1' in text


def test_concurrent_updates_are_not_lost(tmp_path):
    metrics = UsageMetrics(tmp_path / "usage-metrics.json", enabled=True)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _: metrics.increment(
                    "soramimic_usage_outputs_total", {"action": "playback"}
                ),
                range(100),
            )
        )

    assert 'soramimic_usage_outputs_total{action="playback"} 100' in metrics.render_prometheus()


def test_schema_rejects_identifiers_and_free_text(tmp_path):
    metrics = UsageMetrics(tmp_path / "usage-metrics.json", enabled=True)

    with pytest.raises(ValueError, match="metric name"):
        metrics.increment("requests_total", {})
    with pytest.raises(ValueError, match="metric label"):
        metrics.increment("soramimic_usage_requests_total", {"job-id": "deadbeef"})
    with pytest.raises(ValueError, match="metric value"):
        metrics.increment(
            "soramimic_usage_requests_total",
            {"filename": "private song.wav"},
        )

    assert not (tmp_path / "usage-metrics.json").exists()


def test_disabled_store_neither_writes_nor_renders(tmp_path):
    path = tmp_path / "usage-metrics.json"
    metrics = UsageMetrics(path, enabled=False)
    metrics.increment("soramimic_usage_jobs_submitted_total")
    metrics.observe("soramimic_usage_job_duration_seconds", 1.0, (1.0,))

    assert metrics.render_prometheus() == ""
    assert not path.exists()


def test_persisted_shape_contains_aggregates_only(tmp_path):
    path = tmp_path / "usage-metrics.json"
    metrics = UsageMetrics(path, enabled=True)
    metrics.increment(
        "soramimic_usage_jobs_finished_total",
        {"input_kind": "midi", "outcome": "done", "reason": "success"},
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "days"}
    assert data["version"] == 2
    assert "deadbeef" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unreadable_store_is_preserved_and_reported_unhealthy(tmp_path):
    path = tmp_path / "usage-metrics.json"
    path.write_text("not-json", encoding="utf-8")

    metrics = UsageMetrics(path, enabled=True)
    metrics.increment("soramimic_usage_jobs_submitted_total")

    assert metrics.healthy is False
    assert metrics.render_prometheus() == ""
    assert path.read_text(encoding="utf-8") == "not-json"


def test_store_keeps_only_the_latest_ninety_utc_days(tmp_path):
    path = tmp_path / "usage-metrics.json"
    current = [date(2026, 1, 1)]
    metrics = UsageMetrics(path, enabled=True, today=lambda: current[0])
    metrics.increment("soramimic_usage_jobs_submitted_total")

    current[0] = date(2026, 3, 31)
    metrics.increment("soramimic_usage_jobs_submitted_total")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["days"]) == {"2026-01-01", "2026-03-31"}
    assert "soramimic_usage_jobs_submitted_total 2" in metrics.render_prometheus()

    current[0] = date(2026, 4, 1)
    metrics.increment("soramimic_usage_jobs_submitted_total")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["days"]) == {"2026-03-31", "2026-04-01"}
    assert "soramimic_usage_jobs_submitted_total 2" in metrics.render_prometheus()


def test_legacy_aggregate_store_is_migrated_to_current_day(tmp_path):
    path = tmp_path / "usage-metrics.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "counters": {"soramimic_usage_jobs_submitted_total\t[]": 4},
                "histograms": {},
            }
        ),
        encoding="utf-8",
    )

    metrics = UsageMetrics(
        path,
        enabled=True,
        today=lambda: date(2026, 9, 20),
    )

    assert "soramimic_usage_jobs_submitted_total 4" in metrics.render_prometheus()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert set(data["days"]) == {"2026-09-20"}


def test_expired_days_are_removed_from_disk_during_restart(tmp_path):
    path = tmp_path / "usage-metrics.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "days": {
                    "2026-01-01": {
                        "counters": {
                            "soramimic_usage_jobs_submitted_total\t[]": 3,
                        },
                        "histograms": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = UsageMetrics(
        path,
        enabled=True,
        today=lambda: date(2026, 4, 1),
    )

    assert metrics.render_prometheus() == ""
    assert json.loads(path.read_text(encoding="utf-8"))["days"] == {}
