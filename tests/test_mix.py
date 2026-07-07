from pathlib import Path

import mido

from helpers import build_xf_midi
from soramimic_video.mix import make_accompaniment_midi, resolve_accompaniment
from soramimic_video.project import Project, SongInfo
from soramimic_video.xfparse import analyze_midi


def test_make_accompaniment_midi_removes_melody(tmp_path: Path):
    midi_path = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 480, 60), (480, 480, 62)],
        lyric_events=[(0, "あ"), (480, "い")],
    )
    # 伴奏チャンネル(ch1)の音を後から足す
    mid = mido.MidiFile(str(midi_path), clip=True)
    data = midi_path.read_bytes()
    xf_start = data.index(b"XFIH")
    track = mid.tracks[0]
    eot = track.pop()  # end_of_track
    track.append(mido.Message("note_on", channel=1, note=40, velocity=80, time=0))
    track.append(mido.Message("note_off", channel=1, note=40, velocity=64, time=480))
    track.append(eot)
    import io

    buf = io.BytesIO()
    mid.save(file=buf)
    midi_path.write_bytes(buf.getvalue() + data[xf_start:])

    project = analyze_midi(midi_path)
    assert project.song.melody_channel == 0

    out = make_accompaniment_midi(project, tmp_path / "acc.mid")
    acc = mido.MidiFile(str(out), clip=True)
    notes = [m for t in acc.tracks for m in t if m.type in ("note_on", "note_off")]
    assert notes, "伴奏の音符が残っていない"
    assert all(m.channel == 1 for m in notes)
    # デルタ時間の繰り越しでタイミングが保たれている
    total_acc = sum(m.time for m in acc.tracks[0])
    total_src = sum(m.time for m in mido.MidiFile(str(midi_path), clip=True).tracks[0])
    assert total_acc == total_src


def test_resolve_accompaniment_uses_separated_wav(tmp_path: Path):
    """音源プロジェクトでは分離済み伴奏wavをそのまま使う(fluidsynth不要)。"""
    acc = tmp_path / "no_vocals.wav"
    acc.write_bytes(b"RIFF")
    project = Project(
        song=SongInfo(
            midi_path="", ticks_per_beat=480, accompaniment_path=str(acc)
        )
    )
    assert resolve_accompaniment(project, tmp_path, soundfont=None) == acc


def test_resolve_accompaniment_missing_wav_raises(tmp_path: Path):
    project = Project(
        song=SongInfo(
            midi_path="",
            ticks_per_beat=480,
            accompaniment_path=str(tmp_path / "nai.wav"),
        )
    )
    import pytest

    with pytest.raises(RuntimeError, match="伴奏"):
        resolve_accompaniment(project, tmp_path, soundfont=None)
