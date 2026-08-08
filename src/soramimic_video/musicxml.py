"""替え歌歌詞つきMusicXMLの生成(NEUTRINO入力用)。

構造は midi2musicxml で実績のある最小形式:
score-partwise > part > measure > note {pitch, duration, lyric} / rest。
divisions は ticks_per_beat をそのまま使い、曲頭(tick 0)から休符で埋めて
絶対時間を保つ(伴奏とのミックス時に位置合わせが不要になる)。曲頭から音符が始まる曲でも
先頭には必ず休符を置く(NEUTRINOが2秒の休符を勝手に足すのを防ぐ。HEAD_REST_SEC 参照)。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .kana import vowel_of
from .project import Project

logger = logging.getLogger(__name__)

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
        bounds.append(tick + num * ticks_per_beat * 4 // den)
    return bounds


def _head_rest_ticks(project: Project) -> int:
    """HEAD_REST_SEC 相当のtick数(曲頭のテンポで換算)。"""
    tpb = project.song.ticks_per_beat
    us_per_beat = project.song.tempo_map[0][1] if project.song.tempo_map else 500_000
    ticks_per_sec = tpb * 1_000_000 / us_per_beat
    return max(1, round(HEAD_REST_SEC * ticks_per_sec))


def _insert_head_rest(segments: list[_Segment], lead: int) -> None:
    """先頭の音符の頭を休符に置き換える(合計tick=絶対時間は変えない)。

    NEUTRINOは休符始まりでないスコアの頭に2秒の休符を勝手に足すため、曲頭から音符が
    始まる曲では自前で休符を作る。借りるのは lead tick まで、かつ音符の半分まで
    (短い音符を潰さない)。1tickしかなく分けられない音符は丸ごと休符にする。
    """
    head = segments[0]
    lead = min(lead, (head.end - head.start) // 2)
    if lead <= 0:  # 分けられない極短音符(1tick)は休符にする
        head.midi_note, head.lyric = None, None
        return
    head.start += lead
    segments.insert(0, _Segment(0, lead, None))


def build_musicxml(
    project: Project, lyric_map: dict[int, str], transpose: int = 0
) -> str:
    """lyric_map: note_id -> 歌唱カナ。transpose: 半音単位の移調(-12で1オクターブ下)。"""
    tpb = project.song.ticks_per_beat

    # 音符列(重なりは後勝ちでクリップ)+ 休符で埋めた区間列を作る
    sung = sorted(project.notes, key=lambda n: n.start_tick)
    min_rest = tpb // 8  # これ未満の隙間は直前の音符に吸収(極小休符はNEUTRINOに有害)
    segments: list[_Segment] = []
    cursor = 0
    for n in sung:
        start, end = n.start_tick, n.end_tick
        if start < cursor:
            if segments and segments[-1].midi_note is not None:
                segments[-1].end = start  # 前の音符を切り詰め
            start = max(start, cursor)
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
    if segments[0].midi_note is not None:  # 曲頭(tick 0)から音符が始まる曲
        _insert_head_rest(segments, _head_rest_ticks(project))
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

    bounds = _measure_boundaries(project.song.time_signatures, end_tick, tpb)

    # 小節境界で分割(タイ付与)
    def split(seg: _Segment) -> list[_Segment]:
        parts: list[_Segment] = []
        s = seg.start
        for b in bounds:
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

    flat = [p for seg in segments for p in split(seg)]
    if bounds[-1] > end_tick:  # 最終小節の残りを休符で埋める
        flat.append(_Segment(end_tick, bounds[-1], None))

    tempo_changes = sorted(project.song.tempo_map)
    sig_changes = {t: (num, den) for t, num, den in sorted(project.song.time_signatures)}

    root = ET.Element("score-partwise", version="3.1")
    part = ET.SubElement(root, "part", id="P1")
    # part-list はNEUTRINOに不要だが妥当なMusicXMLにするため付ける
    part_list = ET.Element("part-list")
    sp = ET.SubElement(part_list, "score-part", id="P1")
    ET.SubElement(sp, "part-name").text = "vocal"
    root.insert(0, part_list)

    seg_i = 0
    pending_tempi = list(tempo_changes)
    for m_i in range(len(bounds) - 1):
        m_start, m_end = bounds[m_i], bounds[m_i + 1]
        measure = ET.SubElement(part, "measure", number=str(m_i + 1))

        attrs = None
        if m_i == 0 or m_start in sig_changes:
            attrs = ET.SubElement(measure, "attributes")
            if m_i == 0:
                ET.SubElement(attrs, "divisions").text = str(tpb)
                key = ET.SubElement(attrs, "key")
                ET.SubElement(key, "fifths").text = "0"
            num, den = sig_changes.get(m_start, (4, 4)) if m_i > 0 else (
                sig_changes.get(0, (4, 4))
            )
            time = ET.SubElement(attrs, "time")
            ET.SubElement(time, "beats").text = str(num)
            ET.SubElement(time, "beat-type").text = str(den)
            if m_i == 0:
                clef = ET.SubElement(attrs, "clef")
                ET.SubElement(clef, "sign").text = "G"
                ET.SubElement(clef, "line").text = "2"

        # この小節内に入るテンポ変更(小節頭に寄せる。中間の変更は近似)
        while pending_tempi and pending_tempi[0][0] < m_end:
            tick, tempo_us = pending_tempi.pop(0)
            if tick > m_start:
                logger.warning(
                    "小節%dの途中(tick=%d)のテンポ変更を小節頭に寄せました", m_i + 1, tick
                )
            bpm = 60_000_000 / tempo_us
            direction = ET.SubElement(measure, "direction")
            ET.SubElement(direction, "sound", tempo=f"{bpm:.4f}")

        while seg_i < len(flat) and flat[seg_i].start < m_end:
            seg = flat[seg_i]
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
                lyric = ET.SubElement(note_el, "lyric")
                ET.SubElement(lyric, "text").text = seg.lyric
            seg_i += 1

    ET.indent(root)
    return XML_HEADER + ET.tostring(root, encoding="unicode") + "\n"
