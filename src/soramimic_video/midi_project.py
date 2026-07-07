"""生成メロディMIDI(音源なし)から project.json を作る(issue #6, docs手法5)。

ChatMusician等で生成した単旋律MIDIとベース歌詞を入力に、音源もf0も使わず
「1音符=1モーラ」の器を作る。タイミング・ピッチはMIDIそのもの(=確定)。
以降の convert(空耳変換) / synthesize / video はそのまま流せる。伴奏は
メロディMIDIをそのままレンダリングして使う(入力著作物ゼロの構成)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .audio_project import MoraNote, build_project
from .kana import split_moras
from .melody_align import load_midi_notes, monophony_ratio, skyline
from .project import Project
from .reading import text_to_kana

logger = logging.getLogger(__name__)

DEFAULT_LINE_GAP_SEC = 0.4  # これ以上の休符で行(フレーズ)を区切る
DEFAULT_MAX_LINE_NOTES = 12  # 休符が無いとき、この音符数で機械的に改行


def _select_channel(notes_by_channel: dict[int, list]) -> int:
    """最も音数の多い非ドラムチャンネルをメロディとみなす。"""
    candidates = {ch: v for ch, v in notes_by_channel.items() if ch != 9 and v}
    if not candidates:
        raise ValueError("メロディに使えるチャンネルがありません")
    return max(candidates, key=lambda ch: len(candidates[ch]))


def _group_lines(
    notes: list, gap_sec: float, max_line_notes: int
) -> list[list[int]]:
    """音符インデックスを行(フレーズ)にまとめる。

    休符(前の音符の終わりから次の音符の始まりまでの間隔)が gap_sec 以上で改行。
    休符が乏しい旋律のために max_line_notes でも強制改行する。
    """
    lines: list[list[int]] = [[]]
    for i, n in enumerate(notes):
        if lines[-1]:
            prev = notes[lines[-1][-1]]
            if n.start_sec - prev.end_sec >= gap_sec or len(lines[-1]) >= max_line_notes:
                lines.append([])
        lines[-1].append(i)
    return [ln for ln in lines if ln]


def base_kana_stream(lyrics: str | None, n_notes: int) -> list[str]:
    """ベース歌詞から n_notes 個のモーラ列を作る(1音符=1モーラ)。

    歌詞が無ければ「ラ」で埋める。歌詞のモーラが足りなければ繰り返す。
    """
    if not lyrics or not lyrics.strip():
        return ["ラ"] * n_notes
    moras: list[str] = []
    for line in lyrics.splitlines():
        moras.extend(split_moras(text_to_kana(line)))
    moras = [m for m in moras if m.strip()]
    if not moras:
        return ["ラ"] * n_notes
    return [moras[i % len(moras)] for i in range(n_notes)]


def build_from_melody_midi(
    midi_path: Path,
    project_dir: Path,
    lyrics: str | None = None,
    channel: int | None = None,
    gap_sec: float = DEFAULT_LINE_GAP_SEC,
    max_line_notes: int = DEFAULT_MAX_LINE_NOTES,
    render_backing: bool = True,
    soundfont: str | None = None,
) -> Project:
    """生成メロディMIDIから Project を組み立てる。"""
    notes_by_channel = load_midi_notes(midi_path)
    if channel is None:
        channel = _select_channel(notes_by_channel)
    melody = notes_by_channel[channel]
    if monophony_ratio(melody) < 0.95:
        before = len(melody)
        melody = skyline(melody)
        logger.info("和音混じりのためskylineで旋律線化: %d -> %d音", before, len(melody))
    melody = sorted(melody, key=lambda n: n.start_sec)
    logger.info("メロディ: ch%d %d音", channel, len(melody))

    line_groups = _group_lines(melody, gap_sec, max_line_notes)
    kana = base_kana_stream(lyrics, len(melody))

    mora_notes: list[MoraNote] = []
    line_texts: list[str] = []
    for li, group in enumerate(line_groups):
        for idx in group:
            n = melody[idx]
            mora_notes.append(
                MoraNote(
                    line=li,
                    kana=kana[idx],
                    start_sec=n.start_sec,
                    end_sec=n.end_sec,
                    midi_note=n.midi_note,
                )
            )
        line_texts.append("".join(kana[idx] for idx in group))

    # 伴奏: 生成メロディMIDIをそのままレンダリング(著作物ゼロの伴奏)
    accompaniment: Path | None = None
    if render_backing:
        from .mix import render_midi

        try:
            work = project_dir / "backing"
            work.mkdir(parents=True, exist_ok=True)
            accompaniment = render_midi(midi_path, work / "backing.wav", soundfont)
        except RuntimeError as e:
            logger.warning("伴奏レンダリングをスキップ(%s)", e)

    project = build_project(
        audio_path=midi_path,
        vocals_path=None,
        accompaniment_path=accompaniment,
        line_texts=line_texts,
        mora_notes=mora_notes,
    )
    return project
