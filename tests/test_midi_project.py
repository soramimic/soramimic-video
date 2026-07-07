from pathlib import Path

from mido import Message, MidiFile, MidiTrack

from soramimic_video.midi_project import build_from_melody_midi


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
    # 4音符、休符で2フレーズに分かれる。元歌詞は2行(フレーズごとに1行)
    notes = [
        (0, 240, 60), (240, 240, 62),
        (960, 240, 64), (1200, 240, 65),  # 大きな休符のあと
    ]
    midi = _plain_midi(tmp_path / "m.mid", notes)
    project = build_from_melody_midi(
        midi, tmp_path / "proj", lyrics="あい\nうえ", render_backing=False
    )
    assert len(project.notes) == 4
    assert [n.midi_note for n in project.notes] == [60, 62, 64, 65]
    # 各フレーズにその行の読みを配る
    assert [n.kana for n in project.notes] == ["ア", "イ", "ウ", "エ"]
    assert len(project.lines) == 2
    # 元歌詞(字幕)は表層(漢字仮名交じり)を保持する
    assert project.lines[0].original_text == "あい"
    assert project.lines[1].original_text == "うえ"
    # 器: 各行のxf_kanaが音符カナの連結(convertの前提)
    for ln in project.lines:
        assert ln.xf_kana == "".join(project.notes[i].kana for i in ln.note_ids)


def test_build_from_melody_midi_keeps_kanji_surface(tmp_path: Path):
    # 漢字仮名交じりの元歌詞: 字幕は漢字のまま、音符には読み(カナ)が乗る
    notes = [(i * 240, 240, 60 + i) for i in range(4)]
    midi = _plain_midi(tmp_path / "m.mid", notes)
    project = build_from_melody_midi(
        midi, tmp_path / "proj", lyrics="東京", render_backing=False, max_line_notes=8
    )
    assert project.lines[0].original_text == "東京"
    assert "".join(n.kana for n in project.notes).startswith("トー")  # 読みが音符に


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
