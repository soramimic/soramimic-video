import copy
import itertools

import pytest

from soramimic_video import voicevox as vv
from soramimic_video.project import Note, Project, SongInfo


def layered(specs):
    notes = [Note(id=i, midi_note=60 + i, start_tick=round(start / vv.FRAME_RATE * 480),
                  end_tick=round(end / vv.FRAME_RATE * 480), start_sec=start / vv.FRAME_RATE,
                  end_sec=end / vv.FRAME_RATE, line=line, surface=kana, kana=kana, raw="")
             for i, (start, end, kana, line) in enumerate(specs)]
    return Project(SongInfo("", 480), notes=notes, lyric_layers={})


@pytest.mark.parametrize("start", [0, 1, 20])
def test_short_notes_keep_all_units_keys_and_outer_bounds(start):
    value = layered([(start, start + .1, "カ", 0),
                     (start + .1, start + 1, "キ", 0),
                     (start + 1, 40, "ク", 0)])
    before = copy.deepcopy(value)
    score = vv.build_score(value)["notes"]
    sung = [n for n in score if n["key"] is not None]
    assert [n["lyric"] for n in sung] == list("カキク")
    assert [n["key"] for n in sung] == [60, 61, 62]
    assert all(n["frame_length"] >= vv.MIN_ELEMENT_FRAMES for n in score)
    assert sum(n["frame_length"] for n in score) == 40
    assert score[0]["key"] is None
    assert score[0]["frame_length"] == (start if start > 1 else vv.HEAD_REST_FRAMES)
    assert value == before


def test_capacity_does_not_cross_line_boundary():
    value = layered([(20, 21, "カ", 0), (21, 40, "キ", 1)])
    with pytest.raises(ValueError, match="歌詞は保持"):
        vv.build_score(value)


@pytest.mark.parametrize("specs", [
    [(20, 23, "カキ", 0)], [(0, 3, "カ", 0)],
    [(20, 21, "カ", 0), (21, 23, "キ", 0)],
])
def test_truly_insufficient_line_capacity_is_explicit(specs):
    value = layered(specs)
    before = copy.deepcopy(value)
    with pytest.raises(ValueError, match="歌詞は保持"):
        vv.build_score(value)
    assert value == before


@pytest.mark.parametrize("mode", ["front", "back", "first", "auto"])
def test_stack_internal_allocation_keeps_minimum_for_every_mora(mode, monkeypatch):
    monkeypatch.setenv("SORAMIMIC_VIDEO_STACKED_MORA_MODE", mode)
    value = layered([(0, 8, "カキク", 0), (8, 20, "ケ", 0)])
    score = vv.build_score(value)["notes"]
    assert [n["lyric"] for n in score if n["key"] is not None] == list("カキクケ")
    assert all(n["frame_length"] >= 2 for n in score)
    assert sum(n["frame_length"] for n in score) == 20


def test_feasible_layout_matches_legacy_score_exactly(monkeypatch):
    value = layered([(20, 40, "カキ", 0), (45, 60, "ク", 0)])
    # An explicit mode avoids the legacy-only omission heuristic.
    monkeypatch.setenv("SORAMIMIC_VIDEO_STACKED_MORA_MODE", "back")
    legacy = copy.deepcopy(value)
    legacy.lyric_layers = None
    assert vv.build_score(value) == vv.build_score(legacy)


def test_integer_projection_is_minimal_with_fixed_endpoints():
    # Exhaustive small examples verify the objective independently of the solver.
    for middle in itertools.product(range(1, 7), repeat=3):
        preferred = [0, *middle, 8]
        result = vv._minimum_spaced_frames(preferred, [2, 0, 2, 0])
        choices = ([0, *inside, 8] for inside in itertools.product(range(9), repeat=3))
        feasible = [x for x in choices
                    if all(b - a >= gap for a, b, gap in
                           zip(x, x[1:], [2, 0, 2, 0], strict=False))]
        def loss(x, preferred=preferred):
            return sum((a - b) ** 2 for a, b in zip(x, preferred, strict=True))
        assert result[0] == 0 and result[-1] == 8
        assert all(b - a >= gap for a, b, gap in
                   zip(result, result[1:], [2, 0, 2, 0], strict=False))
        assert loss(result) == min(map(loss, feasible))
