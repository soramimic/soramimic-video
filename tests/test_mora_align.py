import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from soramimic_video import mora_align
from soramimic_video.mora_align import (
    AlignedMora,
    build_targets,
    interpolate_missing,
)


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


def test_recognition_window_rejects_infeasible_ctc_capacity_before_alignment(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    emissions = mora_align.CTCEmissions(
        np.zeros((200, 3)), {"<pad>": 0, "カ": 1, "キ": 2}
    )
    monkeypatch.setattr(
        mora_align,
        "_forced_align",
        lambda *args: pytest.fail("capacity must be checked before forced alignment"),
    )

    with pytest.raises(mora_align.CTCWindowCapacityError) as captured:
        mora_align.align_moras_with_variants(
            Path("unused.wav"),
            [[["カ", "カ", "キ"]]],
            device="cpu",
            emissions=emissions,
            line_windows=[(1.0, 1.06)],
        )

    error = captured.value
    assert error.line == 0
    assert error.available_frames == 3
    assert error.target_count == 3
    assert error.adjacent_repeats == 1
    assert error.required_frames == 4


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


def test_recognition_window_on_frame_grid_excludes_frame_at_end(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    matrix = np.zeros((2000, 2))
    emissions = mora_align.CTCEmissions(matrix, {"<pad>": 0, "イ": 1})
    calls = []

    def align(values, targets):
        calls.append(len(values))
        return [SimpleNamespace(start=len(values) - 1, end=len(values), score=.42)]

    monkeypatch.setattr(mora_align, "_forced_align", align)
    result, _ = mora_align.align_moras_with_variants(
        Path("unused.wav"), [[["イ"]]], device="cpu", emissions=emissions,
        line_windows=[(28.84, 37.84)],
    )
    assert calls == [450]
    assert (result[0].start_sec, result[0].end_sec) == pytest.approx((37.82, 37.84))


def test_recognition_windows_fold_floating_point_dust_into_shared_boundary():
    previous_end = 56.900000000000006
    assert mora_align._validated_line_windows([
        (53.48, previous_end), (56.9, 59.58),
    ]) == [
        (53.48, previous_end), (previous_end, 59.58),
    ]


def test_pathological_whole_song_span_is_retried_inside_whisper_window(monkeypatch):
    emissions = object()
    global_alignment = [
        _m(0, 0, "ジ", 17.46, 17.48),
        _m(0, 1, "ツ", 18.22, 18.24),
        _m(0, 2, "リョ", 18.92, 26.04),
        _m(0, 3, "ク", 26.14, 26.16),
    ]
    local_alignment = [
        _m(0, 0, "ジ", 26.02, 26.04),
        _m(0, 1, "ツ", 26.12, 26.14),
        _m(0, 2, "リョ", 26.14, 26.18),
        _m(0, 3, "ク", 26.18, 26.20),
    ]
    calls = []

    def align(*args, **kwargs):
        calls.append(kwargs["line_windows"])
        assert kwargs["emissions"] is emissions
        return local_alignment, [0]

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    repaired, diagnostics = mora_align.retry_pathological_line_alignments(
        Path("unused.wav"),
        [[["ジ", "ツ", "リョ", "ク"]]],
        global_alignment,
        [(26.0, 29.0)],
        device="cpu",
        emissions=emissions,
        phonetic_aliases=True,
    )

    assert calls == [[(26.0, 29.0)]]
    assert [(m.kana, m.start_sec, m.end_sec) for m in repaired] == [
        (m.kana, m.start_sec, m.end_sec) for m in local_alignment
    ]
    assert diagnostics == [{
        "line": 0,
        "window_start_sec": 26.0,
        "window_end_sec": 29.0,
        "reasons": ["mora-span-before-whisper-window"],
        "before_max_mora_span_sec": pytest.approx(7.12),
        "before_line_end_sec": 26.16,
        "after_max_mora_span_sec": pytest.approx(.04),
        "after_line_end_sec": 26.2,
        "status": "replaced",
    }]


def test_alignment_before_whisper_window_alone_does_not_trigger_retry(monkeypatch):
    aligned = [_m(0, 0, "ア", 28.3, 28.32), _m(0, 1, "イ", 29.2, 29.22)]
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: pytest.fail("early-only alignment must stay unchanged"),
    )
    repaired, diagnostics = mora_align.retry_pathological_line_alignments(
        Path("unused.wav"),
        [[["ア", "イ"]]],
        aligned,
        [(30.0, 32.7)],
        device="cpu",
        emissions=object(),
    )
    assert repaired is aligned
    assert diagnostics == []


def test_long_mora_inside_whisper_window_is_not_treated_as_pathological(monkeypatch):
    aligned = [_m(0, 0, "アー", 31.0, 33.0)]
    monkeypatch.setattr(
        mora_align,
        "align_moras_with_variants",
        lambda *args, **kwargs: pytest.fail("in-window long tone must stay unchanged"),
    )
    repaired, diagnostics = mora_align.retry_pathological_line_alignments(
        Path("unused.wav"),
        [[["アー"]]],
        aligned,
        [(30.0, 34.0)],
        device="cpu",
        emissions=object(),
    )
    assert repaired is aligned
    assert diagnostics == []


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
