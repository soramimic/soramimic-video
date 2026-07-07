from pathlib import Path

from mido import Message, MidiFile, MidiTrack

from soramimic_video.midi_project import (
    base_kana_stream,
    build_from_melody_midi,
)


def test_base_kana_stream_placeholder():
    assert base_kana_stream(None, 5) == ["ラ"] * 5
    assert base_kana_stream("   ", 3) == ["ラ"] * 3


def test_base_kana_stream_cycles_lyrics():
    # モーラが足りなければ繰り返して音符数ぶん埋める
    out = base_kana_stream("あい", 5)
    assert out == ["ア", "イ", "ア", "イ", "ア"]


def test_base_kana_stream_truncates():
    out = base_kana_stream("あいうえお", 3)
    assert out == ["ア", "イ", "ウ"]


def _plain_midi(path: Path, notes):
    """普通のSMF(単旋律、ch0)を作る。notes=[(start_tick, dur_tick, midi_note)]。"""
    mid = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    mid.tracks.append(track)
    events: list[tuple[int, Message]] = []
    for start, dur, note in notes:
        events.append((start, Message("note_on", channel=0, note=note, velocity=100)))
        events.append((start + dur, Message("note_off", channel=0, note=note, velocity=64)))
    events.sort(key=lambda e: e[0])
    prev = 0
    for tick, msg in events:
        track.append(msg.copy(time=tick - prev))
        prev = tick
    mid.save(str(path))
    return path


def test_build_from_melody_midi_basic(tmp_path: Path):
    # 4音符、休符で2フレーズに分かれる
    notes = [
        (0, 240, 60), (240, 240, 62),
        (960, 240, 64), (1200, 240, 65),  # 大きな休符のあと
    ]
    midi = _plain_midi(tmp_path / "m.mid", notes)
    project = build_from_melody_midi(
        midi, tmp_path / "proj", lyrics="あいうえ", render_backing=False
    )
    assert len(project.notes) == 4
    assert [n.midi_note for n in project.notes] == [60, 62, 64, 65]
    assert [n.kana for n in project.notes] == ["ア", "イ", "ウ", "エ"]
    # 休符で2行に分かれる
    assert len(project.lines) == 2
    # 器: 各行のxf_kanaが音符カナの連結(convertの前提)
    for ln in project.lines:
        assert ln.xf_kana == "".join(project.notes[i].kana for i in ln.note_ids)


def test_build_from_melody_midi_forces_linebreak(tmp_path: Path):
    # 休符なしで連続、max_line_notesで強制改行
    notes = [(i * 240, 240, 60 + (i % 5)) for i in range(20)]
    midi = _plain_midi(tmp_path / "m.mid", notes)
    project = build_from_melody_midi(
        midi, tmp_path / "proj", render_backing=False, max_line_notes=8
    )
    assert len(project.notes) == 20
    assert len(project.lines) == 3  # 8 + 8 + 4
    assert all(n.kana == "ラ" for n in project.notes)  # 歌詞なし
