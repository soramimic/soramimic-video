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

from .kana import normalize_long_vowels, split_moras
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
    repaired = _repair_word_internal_breaks(result)
    if repaired:
        logger.warning("XFの語中改行を補正しました: %d箇所", repaired)
    return result


def _repair_word_internal_breaks(events: list[LyricEvent]) -> int:
    """XFの明らかに壊れた語中改行を保守的に取り除く。

    実データに ``/止[と]`` ``め`` ``/る`` ``/る[ほ]`` ``ほ`` のような列が
    ある。単独行 ``/る`` の直後に、同じ表層 ``る`` を別の読み ``ほ`` で
    再度出すのは正常な歌詞表記ではない。この特徴が全て揃うときだけ、
    前行・1モーラ行・後行を連結し、後行先頭の重複表層を継続モーラ
    として空にする。その音符は直前のモーラを伸ばす音符なので、読みは
    後続表層の重複ではなく長音に直す。

    単に短い行や ``/あ`` ``/あ`` のよう正常な反復は対象にしない。
    """
    repaired = 0
    for i in range(1, len(events) - 2):
        one = events[i]
        following = events[i + 1]
        one_surface = normalize_kana(one.surface)
        one_kana = normalize_kana(one.kana)
        following_kana = normalize_kana(following.kana)
        if not (
            one.line_break_before
            and following.line_break_before
            and one.surface
            and one.surface == following.surface
            and one.raw == f"/{one.surface}"
            and following.raw == f"/{one.surface}[{following.kana}]"
            and events[i + 2].raw == following.kana
            and one_surface
            and one_surface == one_kana
            and following_kana
            and following_kana != one_kana
        ):
            continue
        one.line_break_before = False
        following.line_break_before = False
        following.surface = ""
        following.kana = "ー"
        # この壊れ方では、同じ行の少し後ろで長音モーラが
        # 次の表層の手前へずれることがある(「い」「意思[い」「し]」)。
        stop = next(
            (j for j in range(i + 2, len(events)) if events[j].line_break_before),
            len(events),
        )
        _repair_shifted_long_vowel(events, i + 2, min(stop, i + 10))
        repaired += 1
    return repaired


def _repair_shifted_long_vowel(events: list[LyricEvent], start: int, stop: int) -> bool:
    """認識済みの破損行内で、後続語の手前にずれた長音を1件直す。"""
    vowels = "あいうえおアイウエオ"
    for j in range(start, min(stop, len(events) - 2)):
        vowel, word, continuation = events[j:j + 3]
        if not (
            not vowel.line_break_before
            and not word.line_break_before
            and not continuation.line_break_before
            and vowel.raw == vowel.surface == vowel.kana
            and vowel.raw in vowels
            and word.surface
            and re.search(r"[\u3400-\u9fff]", word.surface)
            and word.raw == f"{word.surface}[{vowel.kana}"
            and word.kana == vowel.kana
            and continuation.surface == ""
            and continuation.raw.endswith("]")
        ):
            continue
        vowel.surface = word.surface
        word.surface = ""
        word.kana = "ー"
        return True
    return False


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


def _time_signatures(midi: XFMidiFile) -> list[list[int]]:
    sigs: list[list[int]] = []
    for track in midi.tracks:
        for tick, msg in _absolute_events(track):
            if msg.type == "time_signature":
                sigs.append([tick, msg.numerator, msg.denominator])
    sigs.sort()
    if not sigs or sigs[0][0] > 0:
        sigs.insert(0, [0, 4, 4])
    return sigs


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


def _fix_particle_kana(lines: list[Line], notes: list[Note]) -> int:
    """助詞の「は/へ/を」の読みを発音形「ワ/エ/オ」に直す(表記はそのまま)。

    XFの歌詞カナは表記どおりで、助詞の「は」も ``ハ`` のまま入っている
    (市販のXFでも同じ)。この読みは①合成でそのまま歌われる、②替え歌の
    単語照合に使われる、の2箇所に効くので、読み込んだ時点で直しておく。
    形態素解析器が無い環境では何もしない(従来どおりの読みになる)。
    """
    try:
        from .reading import particle_pronunciations
    except ImportError:  # pragma: no cover - 依存が無い環境
        return 0
    fixed = 0
    for line in lines:
        spans: list[tuple[int, int, Note]] = []
        pos = 0
        for i in line.note_ids:
            surface = notes[i].surface
            if surface:
                spans.append((pos, pos + len(surface), notes[i]))
                pos += len(surface)
        try:
            marks = particle_pronunciations(line.xf_surface)
        except RuntimeError as exc:
            logger.warning("助詞の読み補正をスキップします: %s", exc)
            return fixed
        for offset, pron in marks:
            for start, end, note in spans:
                if start <= offset < end:
                    # 1音符=助詞1文字のときだけ触る(結合モーラは対象外)
                    if end - start == 1 and len(note.kana) == 1 and note.kana != pron:
                        note.kana = pron
                        fixed += 1
                    break
        line.xf_kana = "".join(notes[i].kana for i in line.note_ids)
    return fixed


def _distribute_moras(moras: list[str], count: int) -> list[str] | None:
    """モーラ列を空カナ音符へ順序を保って均等配分する。"""
    if count <= 0:
        return [] if not moras else None
    if len(moras) < count:
        return None
    width, extra = divmod(len(moras), count)
    out: list[str] = []
    pos = 0
    for i in range(count):
        size = width + (1 if i < extra else 0)
        out.append("".join(moras[pos:pos + size]))
        pos += size
    return out


def _fill_missing_kana(lines: list[Line], notes: list[Note]) -> int:
    """ルビなし漢字の空カナを、行全体の文脈付き読みから補完する。

    市販XFには ``女`` ``々`` ``し`` ``く`` ``て`` のように、漢字イベントだけ
    読みを持たないデータがある。空カナのままMusicXMLへ渡すとNEUTRINOが有音程
    ノートを「あ」と発声するため、既存カナをアンカーに行読みを切り分ける。
    """
    try:
        from .reading import text_to_kana
    except ImportError:  # pragma: no cover - 読み依存が無い環境
        return 0

    filled = 0
    for line in lines:
        line_notes = [notes[i] for i in line.note_ids]
        if not any(not n.kana for n in line_notes):
            continue
        try:
            full = split_moras(normalize_long_vowels(text_to_kana(line.xf_surface)))
        except RuntimeError as exc:
            logger.warning("ルビなし漢字の読み補完をスキップします: %s", exc)
            return filled
        if not full:
            continue

        assignments: dict[int, str] = {}
        pending: list[int] = []
        cursor = 0
        valid = True
        for i, note in enumerate(line_notes):
            if not note.kana:
                pending.append(i)
                continue
            anchor = split_moras(normalize_long_vowels(note.kana))
            start = cursor + len(pending)
            # XFは助詞を表記どおり「ヲ」と持つことも発音形「オ」と持つこともある。
            # 読み補完のアンカー照合では同じ発音として扱う。
            equivalent = {"ヲ": "オ", "ハ": "ワ", "ヘ": "エ"}
            found = next(
                (
                    p
                    for p in range(start, len(full) - len(anchor) + 1)
                    if [equivalent.get(m, m) for m in full[p:p + len(anchor)]]
                    == [equivalent.get(m, m) for m in anchor]
                ),
                None,
            )
            if found is None:
                valid = False
                break
            distributed = _distribute_moras(full[cursor:found], len(pending))
            if distributed is None:
                valid = False
                break
            assignments.update(zip(pending, distributed, strict=True))
            pending = []
            cursor = found + len(anchor)

        if valid:
            distributed = _distribute_moras(full[cursor:], len(pending))
            if distributed is None:
                valid = False
            else:
                assignments.update(zip(pending, distributed, strict=True))
        if not valid or not assignments:
            continue
        for i, kana in assignments.items():
            line_notes[i].kana = kana
            filled += 1
        line.xf_kana = "".join(n.kana for n in line_notes)
    return filled


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

    fixed = _fix_particle_kana(lines, notes)
    if fixed:
        logger.info("助詞の読みを発音形に補正しました: %d音符 (は→ワ 等)", fixed)
    filled = _fill_missing_kana(lines, notes)
    if filled:
        logger.info("ルビなし漢字の読みを補完しました: %d音符", filled)

    song = SongInfo(
        midi_path=str(midi_path),
        ticks_per_beat=midi.ticks_per_beat,
        melody_channel=melody_channel,
        time_offset=int(info.get("time_offset", 0)),
        language=str(info.get("language", "JP")),
        tempo_map=tempo_map,
        time_signatures=_time_signatures(midi),
    )
    return Project(song=song, notes=notes, lines=lines)
