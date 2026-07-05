"""モーラ時刻列+音高 → project.json(音源プロジェクト)の組み立て。

tick はテンポ復元をせず、固定BPMのテンポマップ1本を置いて
実測秒からの直接換算で決める。NEUTRINO 用 MusicXML は音楽的に正しい
音価を要求しないのでこれで十分で、モーラ間の隙間は musicxml.py の
休符埋めがそのまま機能する。start_sec/end_sec は実測値を保持するので
video / mix のタイミングには換算誤差が影響しない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .project import Line, Note, Project, SongInfo

logger = logging.getLogger(__name__)

DEFAULT_BPM = 120.0
TICKS_PER_BEAT = 480
MIN_NOTE_SEC = 0.05  # これ未満の音符はNEUTRINOに有害なので伸長する


@dataclass
class MoraNote:
    """analyze-audio が確定した歌唱モーラ1つ。"""

    line: int
    kana: str
    start_sec: float
    end_sec: float
    midi_note: int


def sec_to_tick(sec: float, bpm: float = DEFAULT_BPM, tpb: int = TICKS_PER_BEAT) -> int:
    return round(sec * bpm / 60.0 * tpb)


def build_project(
    *,
    audio_path: Path,
    vocals_path: Path | None,
    accompaniment_path: Path | None,
    line_texts: list[str],
    mora_notes: list[MoraNote],
    bpm: float = DEFAULT_BPM,
) -> Project:
    """モーラ音符列から project.json 相当の Project を作る。

    line_texts はモーラの line 番号が指す元歌詞(またはWhisper認識結果)の行。
    モーラの無い行は project.lines に含めない(行IDは詰め直す)。
    """
    moras = sorted(mora_notes, key=lambda m: (m.start_sec, m.line))

    # 時刻の整形: 最小長を確保しつつ次のモーラと重ならないようにする
    for i, m in enumerate(moras):
        if m.end_sec - m.start_sec < MIN_NOTE_SEC:
            m.end_sec = m.start_sec + MIN_NOTE_SEC
        if i + 1 < len(moras):
            next_start = moras[i + 1].start_sec
            if m.end_sec > next_start > m.start_sec:
                m.end_sec = next_start

    notes: list[Note] = []
    line_ids: dict[int, int] = {}  # 元の行番号 -> 詰め直した行ID
    note_ids_by_line: dict[int, list[int]] = {}
    prev_end_tick = 0
    for m in moras:
        start_tick = max(sec_to_tick(m.start_sec, bpm), prev_end_tick)
        end_tick = max(sec_to_tick(m.end_sec, bpm), start_tick + 1)
        line_id = line_ids.setdefault(m.line, len(line_ids))
        note = Note(
            id=len(notes),
            midi_note=m.midi_note,
            start_tick=start_tick,
            end_tick=end_tick,
            start_sec=m.start_sec,
            end_sec=m.end_sec,
            line=line_id,
            surface="",
            kana=m.kana,
            raw=m.kana,
        )
        notes.append(note)
        note_ids_by_line.setdefault(line_id, []).append(note.id)
        prev_end_tick = end_tick

    lines: list[Line] = []
    for orig_line, line_id in sorted(line_ids.items(), key=lambda kv: kv[1]):
        ids = note_ids_by_line[line_id]
        text = line_texts[orig_line]
        lines.append(
            Line(
                id=line_id,
                xf_surface=text,
                xf_kana="".join(notes[i].kana for i in ids),
                note_ids=ids,
                original_text=text,
            )
        )
    skipped = len(line_texts) - len(lines)
    if skipped:
        logger.info("モーラの無い%d行はスキップしました", skipped)

    song = SongInfo(
        midi_path="",
        ticks_per_beat=TICKS_PER_BEAT,
        melody_channel=None,
        tempo_map=[[0, round(60_000_000 / bpm)]],
        time_signatures=[[0, 4, 4]],
        audio_path=str(audio_path),
        vocals_path=str(vocals_path) if vocals_path else None,
        accompaniment_path=str(accompaniment_path) if accompaniment_path else None,
    )
    return Project(song=song, notes=notes, lines=lines)


def write_srt(path: Path, entries: list[tuple[float, float, str]]) -> Path:
    """目視検証用SRTを書き出す(entries: (start_sec, end_sec, text))。"""

    def ts(sec: float) -> str:
        ms = max(0, round(sec * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = [
        f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(entries, start=1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path
