"""合成所要時間の見積り(synth_estimate)のテスト。"""

from __future__ import annotations

from pathlib import Path

from soramimic_video import synth_estimate as se


def test_estimate_uses_default_when_no_history(tmp_path: Path):
    store = tmp_path / "throughput.json"
    assert se.estimate_seconds(store, 30.0) == se.DEFAULT_FACTOR * 30.0
    # 曲長不明(0以下)なら見積れない
    assert se.estimate_seconds(store, 0) is None


def test_record_then_estimate(tmp_path: Path):
    store = tmp_path / "throughput.json"
    # 初回は観測値そのもの: 60秒処理 / 30秒の曲 = 係数2.0
    se.record_run(store, 30.0, 60.0)
    assert se.load_factor(store) == 2.0
    assert se.estimate_seconds(store, 10.0) == 20.0


def test_moving_average_blends_observations(tmp_path: Path):
    store = tmp_path / "throughput.json"
    se.record_run(store, 10.0, 10.0)  # 係数1.0
    se.record_run(store, 10.0, 30.0, alpha=0.5)  # 観測3.0 -> 0.5*1.0 + 0.5*3.0 = 2.0
    assert se.load_factor(store) == 2.0


def test_bad_input_is_ignored(tmp_path: Path):
    store = tmp_path / "throughput.json"
    se.record_run(store, 0.0, 10.0)
    se.record_run(store, 10.0, 0.0)
    assert not store.exists()
    assert se.load_factor(store) == se.DEFAULT_FACTOR
