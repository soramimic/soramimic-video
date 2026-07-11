"""同梱サンプル「故郷(ふるさと)」のXF MIDIを生成する。

詞: 高野辰之 / 曲: 岡野貞一 (いずれも没後70年以上でパブリックドメイン)。
メロディは公知の楽譜(ト長調・3/4拍子)を手打ちしたもので、
既存の打ち込みデータは含まない。

実行: uv run python examples/gen_furusato.py
出力: src/soramimic_video/static/sample/furusato.mid / furusato_lyrics.txt
(Web UIの「サンプル曲をセット」とAPIの /api/sample/* が配信する)
"""

from __future__ import annotations

import io
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

TPB = 480  # ticks per beat
BEAT = TPB
MEAS = TPB * 3  # 3/4拍子
TEMPO = 600_000  # ♩=100

# 音名 → MIDIノート
D4, FS4, G4, A4, B4, C5, D5, E5 = 62, 66, 67, 69, 71, 72, 74, 76

# (歌詞かな, MIDIノート, 長さtick)。None は行区切り。
# 「ゆ→ゆ・う」などの2音は楽譜どおりのメリスマ。
Q, DQ, E8, DH = BEAT, BEAT * 3 // 2, BEAT // 2, MEAS  # 4分/付点4分/8分/付点2分
SCORE: list[tuple[str, int, int] | None] = [
    ("う", G4, Q), ("さ", G4, Q), ("ぎ", G4, Q),
    ("お", A4, DQ), ("い", B4, E8), ("し", A4, Q),
    ("か", B4, Q), ("の", B4, Q), ("や", C5, Q),
    ("ま", D5, DH),
    None,
    ("こ", C5, Q), ("ぶ", D5, Q), ("な", E5, Q),
    ("つ", B4, DQ), ("り", C5, E8), ("し", B4, Q),
    ("か", A4, Q), ("の", A4, Q), ("か", FS4, Q),
    ("わ", G4, DH),
    None,
    ("ゆ", A4, E8), ("う", G4, E8), ("め", A4, Q), ("は", D4, Q),
    ("い", G4, E8), ("い", A4, E8), ("ま", B4, Q), ("も", B4, Q),
    ("め", C5, E8), ("え", B4, E8), ("ぐ", C5, DQ), ("う", E5, E8),
    ("り", D5, E8), ("い", C5, E8), ("て", B4, Q),  # 残り1拍は休符
    None,
    ("わ", D5, Q), ("す", D5, Q), ("れ", D5, Q),
    ("が", G4, DQ), ("た", A4, E8), ("き", B4, Q),
    ("ふ", C5, Q), ("る", C5, Q), ("さ", A4, Q),
    ("と", G4, DH),
]

LYRICS_TEXT = """\
うさぎ追いし かの山
小ぶな釣りし かの川
夢は今も めぐりて
忘れがたき ふるさと
"""

LEAD_IN = MEAS  # 1小節ぶんの前奏(無音)


def _track_chunk_bytes(mid: MidiFile) -> bytes:
    buf = io.BytesIO()
    mid.save(file=buf)
    data = buf.getvalue()
    start = data.index(b"MTrk")
    return data[start:]


def build() -> tuple[bytes, str]:
    notes: list[tuple[int, int, int]] = []  # (start, dur, note)
    lyric_events: list[tuple[int, str]] = []  # (tick, text)
    tick = LEAD_IN
    line_break = False
    for item in SCORE:
        if item is None:
            line_break = True
            # 「て」のあとの4分休符ぶん小節頭まで進める
            tick += -tick % MEAS
            continue
        kana, note, dur = item
        notes.append((tick, dur, note))
        lyric_events.append((tick, ("/" if line_break else "") + kana))
        line_break = False
        tick += dur

    mid = MidiFile(ticks_per_beat=TPB, charset="cp932")
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=TEMPO, time=0))
    track.append(MetaMessage("time_signature", numerator=3, denominator=4, time=0))
    events: list[tuple[int, Message]] = []
    for start, dur, note in notes:
        events.append((start, Message("note_on", channel=0, note=note, velocity=100, time=0)))
        events.append((start + dur, Message("note_off", channel=0, note=note, velocity=64, time=0)))
    events.sort(key=lambda e: e[0])
    prev = 0
    for t, msg in events:
        msg.time = t - prev
        track.append(msg)
        prev = t
    track.append(MetaMessage("end_of_track", time=0))

    # XFIH + XFKM チャンク(tests/helpers.py と同じ合成方法)
    ih = MidiFile(ticks_per_beat=TPB, charset="cp932")
    iht = MidiTrack()
    ih.tracks.append(iht)
    iht.append(MetaMessage("cue_marker", text="$XFhd:", time=0))
    iht.append(MetaMessage("end_of_track", time=0))
    xfih = _track_chunk_bytes(ih).replace(b"MTrk", b"XFIH", 1)

    xf = MidiFile(ticks_per_beat=TPB, charset="cp932")
    xft = MidiTrack()
    xf.tracks.append(xft)
    xft.append(MetaMessage("cue_marker", text="$Lyrc:1:0:JP", time=0))
    prev = 0
    for t, text in lyric_events:
        xft.append(MetaMessage("lyrics", text=text, time=t - prev))
        prev = t
    xft.append(MetaMessage("end_of_track", time=0))
    xfkm = _track_chunk_bytes(xf).replace(b"MTrk", b"XFKM", 1)

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue() + xfih + xfkm, LYRICS_TEXT


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    data, lyrics = build()
    (out_dir / "furusato.mid").write_bytes(data)
    (out_dir / "furusato_lyrics.txt").write_text(lyrics, encoding="utf-8")
    print(f"wrote {out_dir / 'furusato.mid'} ({len(data)} bytes)")
    print(f"wrote {out_dir / 'furusato_lyrics.txt'}")
