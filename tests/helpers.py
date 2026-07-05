"""テスト用の合成XF MIDIビルダ。

著作権のある実楽曲を使わずにXF解析をテストするため、
mido で通常のMIDIを作り、XFKMチャンク(MTrkヘッダを差し替えたもの)を
末尾に連結してXF風のファイルを合成する。
"""

from __future__ import annotations

import io
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack


def _track_chunk_bytes(mid: MidiFile) -> bytes:
    """MidiFileの最初のトラックチャンクのバイト列を返す。"""
    buf = io.BytesIO()
    mid.save(file=buf)
    data = buf.getvalue()
    start = data.index(b"MTrk")
    return data[start:]


def build_xf_midi(
    path: Path,
    notes: list[tuple[int, int, int]],  # (start_tick, duration_tick, midi_note)
    lyric_events: list[tuple[int, str]],  # (tick, text)  テキストはXF形式の断片
    tempo: int = 500000,
    ticks_per_beat: int = 480,
    header: str = "$Lyrc:1:0:JP",
) -> Path:
    mid = MidiFile(ticks_per_beat=ticks_per_beat, charset="cp932")
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=tempo, time=0))
    events: list[tuple[int, Message]] = []
    for start, dur, note in notes:
        events.append((start, Message("note_on", channel=0, note=note, velocity=100, time=0)))
        events.append((start + dur, Message("note_off", channel=0, note=note, velocity=64, time=0)))
    events.sort(key=lambda e: e[0])
    prev = 0
    for tick, msg in events:
        msg.time = tick - prev
        track.append(msg)
        prev = tick
    track.append(MetaMessage("end_of_track", time=0))

    # XFIH(情報ヘッダ)。xfmidoはXFIH→XFKMの順で読むため両方入れる
    ih = MidiFile(ticks_per_beat=ticks_per_beat, charset="cp932")
    iht = MidiTrack()
    ih.tracks.append(iht)
    iht.append(MetaMessage("cue_marker", text="$XFhd:", time=0))
    iht.append(MetaMessage("end_of_track", time=0))
    xfih = _track_chunk_bytes(ih).replace(b"MTrk", b"XFIH", 1)

    xf = MidiFile(ticks_per_beat=ticks_per_beat, charset="cp932")
    xft = MidiTrack()
    xf.tracks.append(xft)
    xft.append(MetaMessage("cue_marker", text=header, time=0))
    prev = 0
    for tick, text in lyric_events:
        xft.append(MetaMessage("lyrics", text=text, time=tick - prev))
        prev = tick
    xft.append(MetaMessage("end_of_track", time=0))
    xfkm = _track_chunk_bytes(xf).replace(b"MTrk", b"XFKM", 1)

    buf = io.BytesIO()
    mid.save(file=buf)
    path.write_bytes(buf.getvalue() + xfih + xfkm)
    return path
