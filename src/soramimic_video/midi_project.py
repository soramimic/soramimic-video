"""生成メロディMIDI(音源なし)から project.json を作る(issue #6, docs手法5)。

ChatMusician等で生成した単旋律MIDIとベース歌詞を入力に、音源もf0も使わず
「1音符=1モーラ」の器を作る。タイミング・ピッチはMIDIそのもの(=確定)。
以降の convert(空耳変換) / synthesize / video はそのまま流せる。伴奏は、
コード/ベース等メロディ以外のチャンネルがあればそれを、無ければメロディ自身を
レンダリングして使う(入力著作物ゼロの構成)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .audio_project import MoraNote, build_project
from .kana import split_moras
from .melody_align import MelodyNote, load_midi_notes, monophony_ratio, skyline
from .project import Project
from .reading import text_to_kana

logger = logging.getLogger(__name__)

DEFAULT_LINE_GAP_SEC = 0.4  # これ以上の休符で行(フレーズ)を区切る
DEFAULT_MAX_LINE_NOTES = 12  # 休符が無いとき、この音符数で機械的に改行


def _select_channel(notes_by_channel: dict[int, list[MelodyNote]]) -> int:
    """メロディらしいチャンネルを選ぶ。

    伴奏付きMIDI(abc2midiはメロディ/ベース/コードを別chに出す)でメロディを選ぶため、
    単旋律度が高く音高が高いものを優先する(コードは和音で単旋律度が低く、
    ベースは音高が低いので落ちる)。単独chならそれを返す。
    """
    candidates = {ch: v for ch, v in notes_by_channel.items() if ch != 9 and v}
    if not candidates:
        raise ValueError("メロディに使えるチャンネルがありません")

    def score(ch: int) -> float:
        notes = candidates[ch]
        mono = monophony_ratio(notes)
        mean_pitch = sum(n.midi_note for n in notes) / len(notes)
        return mono**2 * mean_pitch

    return max(candidates, key=score)


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
    # 元歌詞は行(フレーズ)単位に割り当てる。字幕には表層(漢字仮名交じり)を出し、
    # その読み(カナ)を1音符1モーラでフレーズの音符に配る(足りなければ循環)。
    lyric_lines = [ln for ln in (lyrics or "").splitlines() if ln.strip()]

    mora_notes: list[MoraNote] = []
    line_texts: list[str] = []
    for li, group in enumerate(line_groups):
        if lyric_lines:
            surface = lyric_lines[li % len(lyric_lines)]
            reading = split_moras(text_to_kana(surface)) or ["ラ"]
        else:
            surface = ""  # 歌詞なしはラで充填、字幕も出さない
            reading = ["ラ"]
        for k, idx in enumerate(group):
            n = melody[idx]
            mora_notes.append(
                MoraNote(
                    line=li,
                    kana=reading[k % len(reading)],
                    start_sec=n.start_sec,
                    end_sec=n.end_sec,
                    midi_note=n.midi_note,
                )
            )
        line_texts.append(surface)

    project = build_project(
        audio_path=midi_path,
        vocals_path=None,
        accompaniment_path=None,
        line_texts=line_texts,
        mora_notes=mora_notes,
    )
    # mix が伴奏を作れるよう、元MIDIとメロディチャンネルを記録する
    project.song.midi_path = str(midi_path)
    project.song.melody_channel = channel

    if render_backing:
        project.song.accompaniment_path = _render_backing(
            project, midi_path, channel, project_dir, soundfont
        )
    return project


def _render_backing(
    project: Project, midi_path: Path, channel: int, project_dir: Path,
    soundfont: str | None,
) -> str | None:
    """伴奏wavをレンダリングする。

    コード/ベース等メロディ以外のチャンネルがあればそれを伴奏にする(=本物のBGM)。
    メロディ単独のMIDIならメロディ自身をレンダリングする(薄いが無よりは良い)。
    """
    from .mix import make_accompaniment_midi, render_midi

    work = project_dir / "backing"
    work.mkdir(parents=True, exist_ok=True)
    try:
        acc_mid = make_accompaniment_midi(project, work / "backing.mid")
        import mido

        has_other = any(
            m.type == "note_on" and m.velocity > 0
            for tr in mido.MidiFile(str(acc_mid)).tracks
            for m in tr
        )
        source = acc_mid if has_other else midi_path
        if not has_other:
            logger.info("伴奏チャンネルが無いためメロディをそのまま伴奏にします")
        return str(render_midi(source, work / "backing.wav", soundfont))
    except RuntimeError as e:
        logger.warning("伴奏レンダリングをスキップ(%s)", e)
        return None
