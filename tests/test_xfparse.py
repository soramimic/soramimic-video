from pathlib import Path

from helpers import build_xf_midi
from soramimic_video.xfparse import analyze_midi, normalize_kana, parse_lyric_events


def test_parse_lyric_events_brackets_and_breaks():
    events = [
        (0, "<"),
        (0, "沈[し"),
        (240, "ず]"),
        (480, "む"),
        (720, "/"),
        (740, "溶[と]"),
        (960, "け"),
    ]
    moras = parse_lyric_events(events)
    assert [(m.surface, m.kana) for m in moras] == [
        ("沈", "し"),
        ("", "ず"),
        ("む", "む"),
        ("溶", "と"),
        ("け", "け"),
    ]
    assert moras[0].line_break_before is True  # '<'
    assert moras[3].line_break_before is True  # '/'
    assert moras[1].line_break_before is False


def test_parse_lyric_events_leading_break_in_same_event():
    moras = parse_lyric_events([(0, "あ"), (480, "/い")])
    assert moras[1].line_break_before is True
    assert moras[1].kana == "い"


def test_normalize_kana():
    assert normalize_kana("しズ") == "シズ"
    assert normalize_kana("キャー!") == "キャー"


def test_analyze_midi_basic(tmp_path: Path):
    # 2行: 「沈[しず]む」(3音符) / 「とけ」(2音符)
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64), (1440, 240, 65), (1680, 240, 67)],
        lyric_events=[
            (480, "沈[し"),
            (720, "ず]"),
            (960, "む"),
            (1440, "/と"),
            (1680, "け"),
        ],
    )
    project = analyze_midi(midi)
    assert [n.kana for n in project.notes] == ["シ", "ズ", "ム", "ト", "ケ"]
    assert [n.midi_note for n in project.notes] == [60, 62, 64, 65, 67]
    assert len(project.lines) == 2
    assert project.lines[0].xf_surface == "沈む"
    assert project.lines[0].xf_kana == "シズム"
    assert project.lines[1].note_ids == [3, 4]
    # tempo 500000us/beat, 480tpb -> 1tick = 1/960秒
    assert abs(project.notes[0].start_sec - 0.5) < 1e-6
    assert abs(project.notes[0].end_sec - 0.75) < 1e-6


def test_analyze_midi_multi_mora_note(tmp_path: Path):
    # 「らい」が1音符に載る(2つ目の歌詞イベントに音符がない)
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 480, 62)],
        lyric_events=[(0, "き"), (240, "ら"), (480, "い")],
    )
    project = analyze_midi(midi)
    assert len(project.notes) == 2
    assert project.notes[1].kana == "ライ"
