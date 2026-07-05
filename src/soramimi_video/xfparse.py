"""XF MIDI の解析: 歌唱モーラ(歌詞+音符+タイミング)の抽出。

XFKM チャンクの歌詞イベントは `表記[かな` / `かな]` / `かな` の断片列で、
1イベントが1音符(1歌唱モーラ)に対応する。`/` は改行、`<` は改ページ。
表記が複数モーラにまたがるときは `沈[し` `ず]` のように分割されて届く。
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jaconv
from xfmido import XFMidiFile, extract_xf_karaoke_info

from .project import Line, Note, Project, SongInfo

logger = logging.getLogger(__name__)

# 歌詞イベントと音符開始tickのずれの許容値(拍のこの割合まで)
PAIRING_TOLERANCE_BEATS = 1 / 8

_KANA_RE = re.compile(r"[^ァ-ヺー]")  # カタカナと長音以外


def normalize_kana(text: str) -> str:
    """ひらがな/カタカナ混在のテキストをカタカナの読みに正規化する。"""
    return _KANA_RE.sub("", jaconv.hira2kata(text))


@dataclass
class LyricEvent:
    tick: int
    raw: str
    surface: str  # 括弧の継続中は空文字
    kana: str  # ひらがな/カタカナの生の読み(正規化前)
    line_break_before: bool


def parse_lyric_events(events: list[tuple[int, str]]) -> list[LyricEvent]:
    """(絶対tick, テキスト) の列を歌唱モーラのイベント列にする。

    `/` `<` は行区切りとして次のモーラに畳み込む。
    """
    result: list[LyricEvent] = []
    in_bracket = False
    pending_break = False
    for tick, raw in events:
        text = raw
        # 行区切り記号(単独イベントのことも先頭に付くこともある)
        while text[:1] in ("/", "<"):
            pending_break = True
            text = text[1:]
        if not text:
            continue
        if in_bracket:
            surface = ""
            kana = text
            if "]" in kana:
                kana = kana.split("]")[0]
                in_bracket = False
        elif "[" in text:
            surface, kana = text.split("[", 1)
            if "]" in kana:
                kana = kana.split("]")[0]
            else:
                in_bracket = True
        else:
            surface = text
            kana = text
        result.append(
            LyricEvent(
                tick=tick,
                raw=raw,
                surface=surface,
                kana=kana,
                line_break_before=pending_break,
            )
        )
        pending_break = False
    return result


def _absolute_events(track) -> list[tuple[int, Any]]:
    tick = 0
    out = []
    for msg in track:
        tick += msg.time
        out.append((tick, msg))
    return out


def _tempo_map(midi: XFMidiFile) -> list[list[int]]:
    tempos: list[list[int]] = []
    for track in midi.tracks:
        for tick, msg in _absolute_events(track):
            if msg.type == "set_tempo":
                tempos.append([tick, msg.tempo])
    tempos.sort()
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, [0, 500000])
    return tempos


def tick_to_sec(tick: int, tempo_map: list[list[int]], ticks_per_beat: int) -> float:
    """tempo map(tick昇順)を使って絶対tickを秒に変換する。"""
    sec = 0.0
    prev_tick, prev_tempo = tempo_map[0]
    for t, tempo in tempo_map[1:]:
        if t >= tick:
            break
        sec += (t - prev_tick) * prev_tempo / 1e6 / ticks_per_beat
        prev_tick, prev_tempo = t, tempo
    sec += (tick - prev_tick) * prev_tempo / 1e6 / ticks_per_beat
    return sec


@dataclass
class RawNote:
    channel: int
    note: int
    start_tick: int
    end_tick: int


def _collect_notes(midi: XFMidiFile) -> list[RawNote]:
    notes: list[RawNote] = []
    for track in midi.tracks:
        active: dict[tuple[int, int], int] = {}  # (channel, note) -> start_tick
        for tick, msg in _absolute_events(track):
            if msg.type == "note_on" and msg.velocity > 0:
                active[(msg.channel, msg.note)] = tick
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if key in active:
                    notes.append(RawNote(msg.channel, msg.note, active.pop(key), tick))
    notes.sort(key=lambda n: n.start_tick)
    return notes


def _select_melody_channel(
    notes: list[RawNote],
    lyric_ticks: list[int],
    header_channel: int | None,
    tolerance: int,
) -> int:
    """歌詞イベントのtickと音符開始が最も一致するチャンネルを選ぶ。

    $Lyrcヘッダのチャンネル値(1始まりのはずだが実装差があるため、
    その値と値-1の両方)を優先候補として先に調べる。
    """
    channels = sorted({n.channel for n in notes})
    candidates = []
    if header_channel is not None:
        candidates += [header_channel - 1, header_channel]
    candidates += channels

    def score(ch: int) -> float:
        starts = sorted(n.start_tick for n in notes if n.channel == ch)
        if not starts or not lyric_ticks:
            return 0.0
        hit = 0
        for t in lyric_ticks:
            i = bisect.bisect_left(starts, t - tolerance)
            if i < len(starts) and starts[i] <= t + tolerance:
                hit += 1
        return hit / len(lyric_ticks)

    best_ch, best_score = None, -1.0
    for ch in candidates:
        if ch not in channels:
            continue
        s = score(ch)
        if s > best_score:
            best_ch, best_score = ch, s
        if s >= 0.9:  # 優先候補が十分一致するならそれで確定
            return ch
    if best_ch is None:
        raise ValueError("メロディチャンネルを特定できません(音符がありません)")
    logger.info("メロディチャンネル自動判定: channel=%d (一致率 %.0f%%)", best_ch, best_score * 100)
    return best_ch


def analyze_midi(midi_path: Path) -> Project:
    """XF MIDIを解析してProject(notes/lines/song)を作る。"""
    midi = XFMidiFile(str(midi_path), charset="cp932")
    if midi.xfkm is None:
        raise ValueError(f"{midi_path} にXFKM(カラオケ歌詞)チャンクがありません")

    info: dict = {}
    try:
        info = extract_xf_karaoke_info(str(midi_path))
    except Exception:
        logger.warning("$Lyrcヘッダの解析に失敗(メロディチャンネルは自動判定します)")

    tempo_map = _tempo_map(midi)
    tolerance = max(1, int(midi.ticks_per_beat * PAIRING_TOLERANCE_BEATS))

    raw_events = [
        (tick, msg.text)
        for tick, msg in _absolute_events(midi.xfkm)
        if msg.type == "lyrics"
    ]
    lyric_events = parse_lyric_events(raw_events)
    if not lyric_events:
        raise ValueError("XFKMに歌詞イベントがありません")

    all_notes = _collect_notes(midi)
    melody_channel = _select_melody_channel(
        all_notes,
        [e.tick for e in lyric_events],
        info.get("melody_channel"),
        tolerance,
    )
    melody_notes = [n for n in all_notes if n.channel == melody_channel]

    # 歌詞イベント→音符のペアリング(開始tickの最近傍、許容差あり)
    notes: list[Note] = []
    lines: list[Line] = []
    cur_note_ids: list[int] = []
    used: set[int] = set()

    def close_line() -> None:
        nonlocal cur_note_ids
        if not cur_note_ids:
            return
        lid = len(lines)
        surf = "".join(notes[i].surface for i in cur_note_ids)
        kana = "".join(notes[i].kana for i in cur_note_ids)
        lines.append(Line(id=lid, xf_surface=surf, xf_kana=kana, note_ids=cur_note_ids))
        for i in cur_note_ids:
            notes[i].line = lid
        cur_note_ids = []

    for ev in lyric_events:
        best_i, best_d = None, tolerance + 1
        for i, rn in enumerate(melody_notes):
            if i in used:
                continue
            d = abs(rn.start_tick - ev.tick)
            if d < best_d or (d == best_d and best_i is not None
                              and rn.note > melody_notes[best_i].note):
                best_i, best_d = i, d
        if best_i is None:
            # 1音符に複数モーラが載るケース(「らい」等): 直前音符の区間内なら結合
            if notes and not ev.line_break_before and ev.tick < notes[-1].end_tick + tolerance:
                prev = notes[-1]
                prev.kana += normalize_kana(ev.kana)
                prev.surface += ev.surface
                prev.raw += ev.raw
                continue
            logger.warning(
                "音符が見つからない歌詞イベントをスキップ: %r (tick=%d)", ev.raw, ev.tick
            )
            continue
        used.add(best_i)
        rn = melody_notes[best_i]
        if ev.line_break_before:
            close_line()
        nid = len(notes)
        notes.append(
            Note(
                id=nid,
                midi_note=rn.note,
                start_tick=rn.start_tick,
                end_tick=rn.end_tick,
                start_sec=round(tick_to_sec(rn.start_tick, tempo_map, midi.ticks_per_beat), 4),
                end_sec=round(tick_to_sec(rn.end_tick, tempo_map, midi.ticks_per_beat), 4),
                line=-1,
                surface=ev.surface,
                kana=normalize_kana(ev.kana),
                raw=ev.raw,
            )
        )
        cur_note_ids.append(nid)
    close_line()

    song = SongInfo(
        midi_path=str(midi_path),
        ticks_per_beat=midi.ticks_per_beat,
        melody_channel=melody_channel,
        time_offset=int(info.get("time_offset", 0)),
        language=str(info.get("language", "JP")),
        tempo_map=tempo_map,
    )
    return Project(song=song, notes=notes, lines=lines)
