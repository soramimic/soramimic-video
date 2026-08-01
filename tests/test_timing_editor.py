from pathlib import Path

import pytest

from soramimic_video.project import (
    Line,
    Note,
    Parody,
    ParodyLine,
    ParodyWord,
    Project,
    SongInfo,
)
from soramimic_video.timing_editor import (
    apply_payload,
    build_payload,
    grid_lines,
    sec_to_tick,
    tick_to_sec,
)


def _project(with_parody: bool = False) -> Project:
    # 120BPM/480tpb: 1拍=0.5秒
    notes = [
        Note(id=0, midi_note=60, start_tick=0, end_tick=480,
             start_sec=0.0, end_sec=0.5, line=0, surface="沈", kana="シ", raw="沈[し"),
        Note(id=1, midi_note=62, start_tick=480, end_tick=960,
             start_sec=0.5, end_sec=1.0, line=0, surface="", kana="ズ", raw="ず]"),
        Note(id=2, midi_note=64, start_tick=960, end_tick=1440,
             start_sec=1.0, end_sec=1.5, line=1, surface="夜", kana="ヨル", raw="夜[よる"),
    ]
    lines = [
        Line(id=0, xf_surface="沈", xf_kana="シズ", note_ids=[0, 1]),
        Line(id=1, xf_surface="夜", xf_kana="ヨル", note_ids=[2], original_text="夜"),
    ]
    parody = None
    if with_parody:
        parody = Parody(
            wordlist="test",
            lines=[ParodyLine(line_id=0, words=[ParodyWord(
                surface="シソ", kana="シソ", original="シソ",
                original_surface="シズ", originalkana="シズ", note_ids=[0, 1],
            )])],
        )
    return Project(
        song=SongInfo(midi_path="x.mid", ticks_per_beat=480, tempo_map=[[0, 500000]]),
        notes=notes, lines=lines, parody=parody,
    )


def test_tick_sec_roundtrip() -> None:
    song = _project().song
    assert tick_to_sec(song, 480) == pytest.approx(0.5)
    assert sec_to_tick(song, 1.25) == 1200
    for tick in (0, 137, 480, 5000):
        assert sec_to_tick(song, tick_to_sec(song, tick)) == pytest.approx(tick, abs=1)


def test_tempo_change_is_followed() -> None:
    song = SongInfo(midi_path="x.mid", ticks_per_beat=480,
                    tempo_map=[[0, 500000], [480, 250000]])  # 1拍目0.5秒、以降0.25秒
    assert tick_to_sec(song, 480) == pytest.approx(0.5)
    assert tick_to_sec(song, 960) == pytest.approx(0.75)
    assert sec_to_tick(song, 0.75) == 960


def test_grid_lines_marks_measures_and_beats() -> None:
    grid = grid_lines(_project().song, until_sec=4.0)
    assert [m[1] for m in grid["measures"]][:3] == [1, 2, 3]
    assert grid["measures"][0][0] == pytest.approx(0.0)
    assert grid["measures"][1][0] == pytest.approx(2.0)  # 4拍 x 0.5秒
    assert grid["beats"][:3] == pytest.approx([0.5, 1.0, 1.5])


def test_build_payload_lists_moras_in_time_order() -> None:
    payload = build_payload(_project())
    assert [m["text"] for m in payload["moras"]] == ["シ", "ズ", "ヨル"]
    assert [m["line"] for m in payload["moras"]] == [0, 0, 1]
    assert [m["i"] for m in payload["moras"]] == [0, 1, 0]
    assert payload["moras"][2]["pitch"] == 64
    # 参照音符は既定で編集前の音符
    assert payload["reference"][0] == [0.0, 0.5, 60]
    assert payload["line_texts"]["1"] == "夜"


def test_apply_payload_moves_note_and_keeps_surface() -> None:
    project = _project()
    payload = build_payload(project)
    payload["moras"][0]["start"] = 0.25
    payload["moras"][0]["end"] = 0.75
    payload["moras"][0]["pitch"] = 67
    info = apply_payload(project, payload)

    assert info["parody_dropped"] is False
    moved = project.notes[0]
    assert moved.start_sec == pytest.approx(0.25)
    assert moved.midi_note == 67
    assert moved.start_tick == 240 and moved.end_tick == 720  # 秒からtickを引き直す
    assert moved.surface == "沈"  # XFの表記は引き継ぐ
    assert [ln.note_ids for ln in project.lines] == [[0, 1], [2]]


def test_apply_payload_split_renumbers_and_drops_parody() -> None:
    project = _project(with_parody=True)
    payload = build_payload(project)
    # 「ヨル」を2モーラに割る(GUIの分割/モーラ割付に相当。新規はid無し)
    payload["moras"][2]["text"] = "ヨ"
    payload["moras"][2]["end"] = 1.25
    payload["moras"].append(
        {"line": 1, "text": "ル", "start": 1.25, "end": 1.5, "pitch": 64}
    )
    info = apply_payload(project, payload)

    assert info["notes"] == 4
    assert [n.id for n in project.notes] == [0, 1, 2, 3]
    assert [n.kana for n in project.notes] == ["シ", "ズ", "ヨ", "ル"]
    assert project.lines[1].note_ids == [2, 3]
    assert project.lines[1].xf_kana == "ヨル"
    assert project.notes[3].surface == ""  # 増えたぶんは継続モーラ扱い
    assert info["parody_dropped"] is True and project.parody is None


def test_apply_payload_drops_empty_line_and_renumbers() -> None:
    project = _project()
    payload = build_payload(project)
    for mora in payload["moras"]:  # 全部を行0へ寄せる
        mora["line"] = 0
    apply_payload(project, payload)

    assert len(project.lines) == 1
    assert project.lines[0].id == 0
    assert project.lines[0].note_ids == [0, 1, 2]
    assert {n.line for n in project.notes} == {0}


def test_apply_payload_rejects_empty() -> None:
    with pytest.raises(ValueError):
        apply_payload(_project(), {"moras": []})


def test_saved_project_reloads(tmp_path: Path) -> None:
    project = _project()
    payload = build_payload(project)
    payload["moras"][1]["end"] = 0.9
    apply_payload(project, payload)
    project.save(tmp_path)

    reloaded = Project.load(tmp_path)
    assert reloaded.notes[1].end_sec == pytest.approx(0.9)
    assert len(reloaded.lines) == 2
