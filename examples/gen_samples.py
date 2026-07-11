"""同梱サンプル曲のXF MIDIと元歌詞を生成する。

いずれも詞・曲ともパブリックドメインの童謡・唱歌で、メロディは公知の
楽譜を手打ちしたもの。既存の打ち込みデータは含まない。
1音符=1モーラに正規化してある(合成エンジンが1音符に複数モーラを
載せられないため。「ももたろうさん」→「ももたろさん」は実際の歌い方)。

実行: uv run python examples/gen_samples.py
出力: src/soramimic_video/static/sample/<id>.mid / <id>_lyrics.txt / samples.json
(Web UIの「サンプル曲をセット」とAPIの /api/samples, /api/sample/* が配信する)
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

TPB = 480  # ticks per beat

# 音名 → MIDIノート
AS3 = 58
C4, D4, DS4, E4, F4, FS4, G4, A4, AS4, B4 = 60, 62, 63, 64, 65, 66, 67, 69, 70, 71
C5, D5, DS5, E5 = 72, 74, 75, 76

Q, DQ, E8, DE, S16 = 480, 720, 240, 360, 120  # 4分/付点4分/8分/付点8分/16分

# 各曲: (歌詞かな, MIDIノート, 長さtick) の列。None は行区切り(次の音符から新しい行)。
# 行区切りでは次の小節頭まで休符を入れる。
SONGS: dict[str, dict] = {
    "furusato": {
        "title": "ふるさと",
        "tempo": 600_000,  # ♩=100
        "time": (3, 4),
        # ト長調。「ゆ→ゆ・う」などの2音は楽譜どおりのメリスマ
        "score": [
            ("う", G4, Q), ("さ", G4, Q), ("ぎ", G4, Q),
            ("お", A4, DQ), ("い", B4, E8), ("し", A4, Q),
            ("か", B4, Q), ("の", B4, Q), ("や", C5, Q),
            ("ま", D5, 1440),
            None,
            ("こ", C5, Q), ("ぶ", D5, Q), ("な", E5, Q),
            ("つ", B4, DQ), ("り", C5, E8), ("し", B4, Q),
            ("か", A4, Q), ("の", A4, Q), ("か", FS4, Q),
            ("わ", G4, 1440),
            None,
            ("ゆ", A4, E8), ("う", G4, E8), ("め", A4, Q), ("は", D4, Q),
            ("い", G4, E8), ("い", A4, E8), ("ま", B4, Q), ("も", B4, Q),
            ("め", C5, E8), ("え", B4, E8), ("ぐ", C5, DQ), ("う", E5, E8),
            ("り", D5, E8), ("い", C5, E8), ("て", B4, Q),  # 残り1拍は休符
            None,
            ("わ", D5, Q), ("す", D5, Q), ("れ", D5, Q),
            ("が", G4, DQ), ("た", A4, E8), ("き", B4, Q),
            ("ふ", C5, Q), ("る", C5, Q), ("さ", A4, Q),
            ("と", G4, 1440),
        ],
        "lyrics": (
            "うさぎ追いし かの山\n小ぶな釣りし かの川\n夢は今も めぐりて\n忘れがたき ふるさと\n"
        ),
    },
    "akatombo": {
        "title": "赤とんぼ",
        "tempo": 666_000,  # ♩≒90
        "time": (3, 4),
        # 変ホ長調。「けぇ」「のぉ」などは楽譜どおりのメリスマ(小書きは大書きに正規化)
        "score": [
            ("ゆ", AS3, E8), ("う", DS4, E8), ("や", DS4, DQ), ("け", F4, E8),
            ("こ", G4, E8), ("や", AS4, E8), ("け", DS5, E8), ("え", C5, E8), ("の", AS4, Q),
            ("あ", C5, E8), ("か", DS4, E8), ("と", DS4, Q),
            ("ん", F4, Q), ("ぼ", G4, 1440),
            None,
            ("お", G4, E8), ("わ", C5, E8), ("れ", AS4, DQ), ("て", C5, E8),
            ("み", DS5, E8), ("た", C5, E8), ("の", AS4, E8), ("お", C5, E8),
            ("は", AS4, E8), ("あ", G4, E8),
            ("い", AS4, E8), ("つ", G4, E8), ("の", DS4, E8), ("お", G4, E8),
            ("ひ", F4, E8), ("い", DS4, E8),
            ("か", DS4, 1440),
        ],
        "lyrics": "夕焼小焼の 赤とんぼ\n負われて見たのは いつの日か\n",
    },
    "momotarou": {
        "title": "桃太郎",
        "tempo": 500_000,  # ♩=120
        "time": (4, 4),
        # ニ長調。「ももたろさん」は歌唱慣行どおり(元歌詞は「桃太郎さん」)
        "score": [
            ("も", A4, DQ), ("も", B4, E8), ("た", A4, E8), ("ろ", A4, E8),
            ("さ", FS4, E8), ("ん", FS4, E8),
            ("も", A4, E8), ("も", A4, E8), ("た", FS4, E8), ("ろ", D4, E8),
            ("さ", E4, Q), ("ん", E4, E8),  # 残り8分は休符
            None,
            ("お", D4, E8), ("こ", D4, E8), ("し", E4, E8), ("に", E4, E8),
            ("つ", FS4, E8), ("け", FS4, E8), ("た", E4, Q),
            ("き", FS4, E8), ("び", FS4, E8), ("だ", B4, E8), ("ん", B4, E8),
            ("ご", A4, DQ),  # 残り8分は休符
            None,
            ("ひ", D5, E8), ("と", D5, E8), ("つ", A4, Q),
            ("わ", FS4, E8), ("た", FS4, E8), ("し", B4, E8), ("に", B4, E8),
            ("く", A4, E8), ("だ", A4, E8), ("さ", FS4, E8), ("い", E4, E8),
            ("な", D4, DQ),
        ],
        "lyrics": "桃太郎さん 桃太郎さん\nお腰につけた きびだんご\n一つわたしに くださいな\n",
    },
    "katatsumuri": {
        "title": "かたつむり",
        "tempo": 500_000,  # ♩=120
        "time": (4, 4),
        # ニ長調・付点のはずむリズム
        "score": [
            ("で", A4, DE), ("ん", A4, S16), ("で", A4, E8), ("ん", FS4, E8),
            ("む", D4, DE), ("し", D4, S16), ("む", D4, E8), ("し", E4, E8),
            ("か", FS4, DE), ("た", FS4, S16), ("つ", E4, E8), ("む", D4, E8),
            ("り", E4, Q),  # 残りは休符
            None,
            ("お", FS4, DE), ("ま", G4, S16), ("え", A4, E8), ("の", B4, E8),
            ("あ", A4, DE), ("た", A4, S16), ("ま", A4, E8), ("は", FS4, E8),
            ("ど", E4, DE), ("こ", E4, S16), ("に", D4, E8), ("あ", E4, E8),
            ("る", FS4, Q),  # 残りは休符
            None,
            ("つ", A4, E8), ("の", D5, E8), ("だ", D5, E8), ("せ", A4, E8),
            ("や", FS4, E8), ("り", A4, E8), ("だ", A4, E8), ("せ", FS4, E8),
            ("あ", D4, E8), ("た", FS4, E8), ("ま", FS4, DE), ("だ", E4, S16),
            ("せ", D4, Q),
        ],
        "lyrics": (
            "でんでんむしむし かたつむり\nお前のあたまは どこにある\nつの出せ槍出せ あたま出せ\n"
        ),
    },
}


def _track_chunk_bytes(mid: MidiFile) -> bytes:
    buf = io.BytesIO()
    mid.save(file=buf)
    data = buf.getvalue()
    start = data.index(b"MTrk")
    return data[start:]


def build(song: dict) -> bytes:
    num, den = song["time"]
    meas = TPB * num * 4 // den
    lead_in = meas  # 1小節ぶんの前奏(無音)
    notes: list[tuple[int, int, int]] = []  # (start, dur, note)
    lyric_events: list[tuple[int, str]] = []  # (tick, text)
    tick = lead_in
    line_break = False
    for item in song["score"]:
        if item is None:
            line_break = True
            tick += -tick % meas  # 次の小節頭まで進める(行末の休符)
            continue
        kana, note, dur = item
        notes.append((tick, dur, note))
        lyric_events.append((tick, ("/" if line_break else "") + kana))
        line_break = False
        tick += dur

    mid = MidiFile(ticks_per_beat=TPB, charset="cp932")
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=song["tempo"], time=0))
    track.append(MetaMessage("time_signature", numerator=num, denominator=den, time=0))
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
    return buf.getvalue() + xfih + xfkm


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sid, song in SONGS.items():
        data = build(song)
        (out_dir / f"{sid}.mid").write_bytes(data)
        (out_dir / f"{sid}_lyrics.txt").write_text(song["lyrics"], encoding="utf-8")
        manifest.append({"id": sid, "title": song["title"]})
        print(f"wrote {sid}.mid ({len(data)} bytes)")
    (out_dir / "samples.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote samples.json ({len(manifest)} songs)")
