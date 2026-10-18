"""替え歌歌詞つきMusicXMLの生成(書き出し・NEUTRINO入力共通)。

構造は midi2musicxml で実績のある最小形式:
score-partwise > part > measure > note {pitch, duration, lyric} / rest。
divisions は原則 ticks_per_beat を使い、曲頭(tick 0)から休符で埋めて
絶対時間を保つ(伴奏とのミックス時に位置合わせが不要になる)。曲頭から音符が始まる曲でも
先頭には必ず休符を置く(NEUTRINOが2秒の休符を勝手に足すのを防ぐ。HEAD_REST_SEC 参照)。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .kana import vowel_of
from .project import Project

_STEPS = [
    ("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0),
    ("F", 1), ("G", 0), ("G", 1), ("A", 0), ("A", 1), ("B", 0),
]

# 曲頭から音符が始まるとき、先頭に置く休符の長さ(秒)。NEUTRINOのmusicXMLtoLabelは
# 休符で始まらないスコアに2秒の休符を勝手に足す("The score does not start with a rest.
# Added a rest")ため、そのままだと歌が2秒遅れて伴奏とずれる。休符を「足す」と同じく
# 絶対時間がずれるので、先頭音符の頭から借りる。NEUTRINOは子音をこの休符から取り、
# 母音は休符の終わり+約11msに来る(実測)ので、短いほど1音目の遅れが小さい。ただし
# 極端に短い休符はラベルが潰れるため、30ms程度を目安にする。
HEAD_REST_SEC = 0.03

XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN"'
    ' "http://www.musicxml.org/dtds/partwise.dtd">\n'
)


@dataclass
class _Segment:
    start: int
    end: int
    midi_note: int | None  # Noneは休符
    lyric: str | None = None
    tie: str | None = None  # None | "start" | "stop" | "continue"


def _measure_boundaries(time_signatures: list[list[int]], end_tick: int,
                        ticks_per_beat: int) -> list[int]:
    """tick 0 から end_tick までの小節境界(先頭0を含む)を返す。"""
    bounds = [0]
    sigs = sorted(time_signatures)
    i = 0
    while bounds[-1] < end_tick:
        tick = bounds[-1]
        while i + 1 < len(sigs) and sigs[i + 1][0] <= tick:
            i += 1
        _, num, den = sigs[i]
        next_tick = tick + num * ticks_per_beat * 4 // den
        if i + 1 < len(sigs):
            next_tick = min(next_tick, sigs[i + 1][0])
        bounds.append(next_tick)
    return bounds


def _head_rest_ticks(project: Project) -> int:
    """HEAD_REST_SEC 相当のtick数(曲頭のテンポで換算)。"""
    tpb = project.song.ticks_per_beat
    us_per_beat = {tick: tempo for tick, tempo in project.song.tempo_map}.get(0, 500_000)
    ticks_per_sec = tpb * 1_000_000 / us_per_beat
    return max(1, round(HEAD_REST_SEC * ticks_per_sec))


def _insert_head_rest(segments: list[_Segment], lead: int) -> None:
    """先頭の音符の頭を休符に置き換える(合計tick=絶対時間は変えない)。

    NEUTRINOは休符始まりでないスコアの頭に2秒の休符を勝手に足すため、曲頭から音符が
    始まる曲では自前で休符を作る。借りるのは lead tick まで、かつ音符の半分まで
    (短い音符を潰さない)。呼び出し側で、分割できるだけのdivisionsを確保する。
    """
    head = segments[0]
    lead = min(lead, (head.end - head.start) // 2)
    head.start += lead
    segments.insert(0, _Segment(0, lead, None))


def build_musicxml(
    project: Project, lyric_map: dict[int, str], transpose: int = 0
) -> str:
    """Build a standard kana score usable by NEUTRINO; transpose is in semitones."""
    tpb = project.song.ticks_per_beat

    # 音符列(重なりは後勝ちでクリップ)+ 休符で埋めた区間列を作る
    sung = sorted(project.notes, key=lambda n: n.start_tick)
    # 共通スコアのフレージングとして極小休符を直前の音符に吸収する。
    min_rest = tpb // 8
    segments: list[_Segment] = []
    cursor = 0
    for n in sung:
        start, end = n.start_tick, n.end_tick
        if start < 0 or end <= start:
            raise ValueError("MusicXML export needs a nonnegative start and positive note duration")
        if start < cursor:
            while segments and segments[-1].start >= start:
                segments.pop()
            if segments:
                segments[-1].end = start
            cursor = start
        if start > cursor:
            if segments and segments[-1].midi_note is not None and start - cursor < min_rest:
                segments[-1].end = start
            else:
                segments.append(_Segment(cursor, start, None))
        if end > start:
            lyric = lyric_map.get(n.id)
            # build_lyric_map が明示的に空文字を返した音符は、XFのルビ欠落などで
            # 発音を特定できなかった音符。歌詞なしの有音程ノートをNEUTRINOへ渡すと
            # 「あ」と発声されるため、推測せず休符にする。キー自体が無い場合は、
            # 歌詞なしスコアを組み立てる既存の直接呼び出しとして従来どおり扱う。
            midi_note = None if n.id in lyric_map and not lyric else n.midi_note
            segments.append(_Segment(start, end, midi_note, lyric))
            cursor = end

    if not segments:
        raise ValueError("音符がありません")
    tick_scale = 1
    if segments[0].midi_note is not None:  # 曲頭(tick 0)から音符が始まる曲
        if segments[0].end - segments[0].start == 1:
            # 1tickの歌詞も失わずに、半tickずつ休符と音符へ分ける。
            tick_scale = 2
            tpb *= tick_scale
            for seg in segments:
                seg.start *= tick_scale
                seg.end *= tick_scale
        _insert_head_rest(segments, _head_rest_ticks(project) * tick_scale)
    end_tick = segments[-1].end

    # 「ー」で始まる歌詞は直前の音の母音を引き継ぐが、休符直後や行頭では
    # 引き継ぐ音が無くNEUTRINOで未定義音素になるため、母音(無ければア)に置換する
    prev_lyric: str | None = None
    prev_was_rest = True
    for seg in segments:
        if seg.midi_note is None:
            prev_was_rest = True
            continue
        if seg.lyric:
            if prev_was_rest and seg.lyric.startswith("ー"):
                v = vowel_of(prev_lyric or "") or "ア"
                seg.lyric = v + seg.lyric[1:]
            prev_lyric = seg.lyric
        prev_was_rest = False

    tempo_changes = {0: 500_000}
    tempo_changes.update({t * tick_scale: tempo for t, tempo in project.song.tempo_map})
    sig_changes = {0: (4, 4)}
    sig_changes.update({t * tick_scale: (num, den)
                        for t, num, den in project.song.time_signatures})
    bounds = _measure_boundaries(
        [[t, num, den] for t, (num, den) in sorted(sig_changes.items())], end_tick, tpb,
    )
    split_ticks = sorted(set(bounds) | set(tempo_changes))

    # 小節境界・テンポ変更点で分割(タイ付与)。directionはその位置で出力する。
    def split(seg: _Segment) -> list[_Segment]:
        parts: list[_Segment] = []
        s = seg.start
        for b in split_ticks:
            if s < b < seg.end:
                parts.append(_Segment(s, b, seg.midi_note))
                s = b
        parts.append(_Segment(s, seg.end, seg.midi_note))
        if len(parts) > 1 and seg.midi_note is not None:
            parts[0].tie = "start"
            for p in parts[1:-1]:
                p.tie = "continue"
            parts[-1].tie = "stop"
        parts[0].lyric = seg.lyric
        return parts

    if bounds[-1] > end_tick:  # 最終小節の残りを休符で埋める
        segments.append(_Segment(end_tick, bounds[-1], None))
    flat = [p for seg in segments for p in split(seg)]

    root = ET.Element("score-partwise", version="3.1")
    part = ET.SubElement(root, "part", id="P1")
    # part-list はNEUTRINOに不要だが妥当なMusicXMLにするため付ける
    part_list = ET.Element("part-list")
    sp = ET.SubElement(part_list, "score-part", id="P1")
    ET.SubElement(sp, "part-name").text = "vocal"
    root.insert(0, part_list)

    seg_i = 0
    current_signature = sig_changes[0]
    for m_i in range(len(bounds) - 1):
        m_start, m_end = bounds[m_i], bounds[m_i + 1]
        measure = ET.SubElement(part, "measure", number=str(m_i + 1))
        current_signature = sig_changes.get(m_start, current_signature)
        num, den = current_signature
        if m_end - m_start < num * tpb * 4 // den:
            measure.set("implicit", "yes")

        attrs = None
        if m_i == 0 or m_start in sig_changes:
            attrs = ET.SubElement(measure, "attributes")
            if m_i == 0:
                ET.SubElement(attrs, "divisions").text = str(tpb)
                key = ET.SubElement(attrs, "key")
                ET.SubElement(key, "fifths").text = "0"
            time = ET.SubElement(attrs, "time")
            ET.SubElement(time, "beats").text = str(num)
            ET.SubElement(time, "beat-type").text = str(den)
            if m_i == 0:
                clef = ET.SubElement(attrs, "clef")
                ET.SubElement(clef, "sign").text = "G"
                ET.SubElement(clef, "line").text = "2"

        while seg_i < len(flat) and flat[seg_i].start < m_end:
            seg = flat[seg_i]
            if seg.start in tempo_changes:
                bpm = f"{60_000_000 / tempo_changes[seg.start]:.8f}".rstrip("0").rstrip(".")
                direction = ET.SubElement(measure, "direction")
                direction_type = ET.SubElement(direction, "direction-type")
                metronome = ET.SubElement(direction_type, "metronome")
                ET.SubElement(metronome, "beat-unit").text = "quarter"
                ET.SubElement(metronome, "per-minute").text = bpm
                ET.SubElement(direction, "sound", tempo=bpm)
            note_el = ET.SubElement(measure, "note")
            if seg.midi_note is None:
                ET.SubElement(note_el, "rest")
            else:
                midi_note = seg.midi_note + transpose
                step, alter = _STEPS[midi_note % 12]
                pitch = ET.SubElement(note_el, "pitch")
                ET.SubElement(pitch, "step").text = step
                if alter:
                    ET.SubElement(pitch, "alter").text = str(alter)
                ET.SubElement(pitch, "octave").text = str(midi_note // 12 - 1)
            ET.SubElement(note_el, "duration").text = str(seg.end - seg.start)
            if seg.tie in ("start", "continue"):
                ET.SubElement(note_el, "tie", type="start")
            if seg.tie in ("stop", "continue"):
                ET.SubElement(note_el, "tie", type="stop")
            if seg.tie is not None:
                notations = ET.SubElement(note_el, "notations")
                if seg.tie in ("stop", "continue"):
                    ET.SubElement(notations, "tied", type="stop")
                if seg.tie in ("start", "continue"):
                    ET.SubElement(notations, "tied", type="start")
            if seg.midi_note is not None and seg.lyric:
                lyric_el = ET.SubElement(note_el, "lyric")
                ET.SubElement(lyric_el, "text").text = seg.lyric
            seg_i += 1

    ET.indent(root)
    return XML_HEADER + ET.tostring(root, encoding="unicode") + "\n"
