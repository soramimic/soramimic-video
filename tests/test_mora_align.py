import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from soramimic_video import mora_align
from soramimic_video.mora_align import AlignedMora, build_targets, interpolate_missing


def test_build_targets_maps_tokens_to_moras():
    vocab = {"シ": 10, "ズ": 11, "キ": 12, "ャ": 13}
    targets, owners = build_targets([["シ", "ズ"], ["キャ"]], vocab)
    assert targets == [10, 11, 12, 13]
    assert owners == [(0, 0), (0, 1), (1, 0), (1, 0)]


def test_build_targets_falls_back_to_hiragana():
    vocab = {"し": 5}
    targets, owners = build_targets([["シ"]], vocab)
    assert targets == [5]
    assert owners == [(0, 0)]


def test_build_targets_skips_unknown_chars():
    vocab = {"ア": 1}
    targets, owners = build_targets([["ア", "ー"]], vocab)
    assert targets == [1]
    assert owners == [(0, 0)]


def _m(line: int, mora: int, kana: str, start: float, end: float) -> AlignedMora:
    return AlignedMora(
        line=line, mora=mora, kana=kana, start_sec=start, end_sec=end, score=1.0
    )


def test_interpolate_missing_uses_neighbors():
    moras = [
        _m(0, 0, "ア", 1.0, 1.5),
        _m(0, 1, "ー", -1.0, -1.0),  # スパンなし
        _m(0, 2, "イ", 2.0, 2.5),
    ]
    interpolate_missing(moras)
    assert moras[1].start_sec == 1.5
    assert moras[1].end_sec == 2.0


def test_interpolate_missing_at_edges():
    moras = [
        _m(0, 0, "ー", -1.0, -1.0),
        _m(0, 1, "ア", 1.0, 1.5),
        _m(0, 2, "ー", -1.0, -1.0),
    ]
    interpolate_missing(moras)
    assert moras[0].start_sec == 0.0
    assert moras[0].end_sec == 1.0
    assert moras[2].start_sec == 1.5
    assert moras[2].end_sec == 1.5


def test_recognition_windows_keep_absolute_frame_times_and_do_not_recompute_models(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    matrix = np.arange(1000 * 3).reshape(1000, 3)
    emissions = mora_align.CTCEmissions(matrix, {"<pad>": 0, "カ": 1, "キ": 2})
    calls = []

    def align(values, targets):
        calls.append((int(values[0, 0] // 3), len(values), targets))
        return [SimpleNamespace(start=0, end=len(values), score=.42)]

    monkeypatch.setattr(mora_align, "_forced_align", align)
    monkeypatch.setattr(mora_align, "compute_emissions",
                        lambda *a: pytest.fail("cached emissions required"))
    result, chosen = mora_align.align_moras_with_variants(
        Path("unused.wav"), [[["カ"]], [["キ"]]], device="cpu", emissions=emissions,
        line_windows=[(2.011, 2.099), (6., 6.1)],
    )
    assert calls == [(126, 4, [1]), (325, 5, [2])]
    assert chosen == [0, 0]
    assert [(m.line, m.mora, m.kana) for m in result] == [(0, 0, "カ"), (1, 0, "キ")]
    assert (result[0].start_sec, result[0].end_sec) == pytest.approx((2.02, 2.099))
    assert (result[1].start_sec, result[1].end_sec) == pytest.approx((6., 6.1))
    assert all(m.score == .42 for m in result)
    np.testing.assert_array_equal(emissions.log_probs, matrix)


@pytest.mark.parametrize("windows", [
    [], [(0, 1)], [(1, 2), (1.5, 3)], [(2, 3), (0, 1)],
    [(-1, 1), (2, 3)], [(0, float("inf")), (2, 3)], [(1, 1), (2, 3)],
    [(0, 1), (10, 11)], [(0.001, 0.002), (2, 3)],
])
def test_invalid_or_empty_recognition_windows_fail_without_dropping_lyrics(monkeypatch, windows):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    emissions = mora_align.CTCEmissions(np.zeros((400, 2)), {"<pad>": 0, "カ": 1})
    monkeypatch.setattr(mora_align, "_forced_align",
                        lambda *a: pytest.fail("validate all windows first"))
    with pytest.raises(ValueError):
        mora_align.align_moras_with_variants(
            Path("unused.wav"), [[["カ"]], [["カ"]]], device="cpu", emissions=emissions,
            line_windows=windows,
        )
