"""NEUTRINOの進捗出力パース(neutrino.parse_progress)のテスト。"""

from __future__ import annotations

from soramimic_video.neutrino import parse_progress


def test_parse_progress_typical():
    assert parse_progress("    progress = 42 % (18.1 / 43.2 sec)") == 0.42


def test_parse_progress_bounds():
    assert parse_progress("progress = 0 %") == 0.0
    assert parse_progress("    progress = 100 % (43.2 / 43.2 sec)") == 1.0


def test_parse_progress_non_progress_lines_return_none():
    assert parse_progress("load models                         : 0.000 [sec]") is None
    assert parse_progress("finish                              : 41.787 [sec]") is None
    assert parse_progress("") is None
