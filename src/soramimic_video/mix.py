"""ミックスステージ: 元MIDIの伴奏(メロディ消音)+ NEUTRINO歌唱 → song.wav。

伴奏はメロディチャンネルのnoteイベントを除いたMIDIをfluidsynthでレンダリングする。
vocal.wav は曲頭(tick 0)からレンダリングされているので、そのまま重ねられる。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import mido

from . import runproc
from .project import Project
from .synthesize import vocal_path

logger = logging.getLogger(__name__)

MIX_DIR = "mix"


def make_accompaniment_midi(project: Project, out_path: Path) -> Path:
    src = mido.MidiFile(project.song.midi_path, clip=True)
    melody = project.song.melody_channel
    for track in src.tracks:
        removed: list = []
        tick_carry = 0
        new_msgs = []
        for msg in track:
            time = msg.time + tick_carry
            tick_carry = 0
            if (
                msg.type in ("note_on", "note_off")
                and getattr(msg, "channel", None) == melody
            ):
                tick_carry = time  # イベントを消してデルタ時間は次に繰り越す
                removed.append(msg)
                continue
            new_msgs.append(msg.copy(time=time))
        track[:] = new_msgs
        if removed:
            logger.debug("%d noteイベントをメロディch=%sから除去", len(removed), melody)
    src.save(str(out_path))
    return out_path


def render_midi(midi_path: Path, wav_path: Path, soundfont: str | None) -> Path:
    fluidsynth = shutil.which("fluidsynth")
    if fluidsynth is None:
        raise RuntimeError("fluidsynth が見つかりません(brew install fluidsynth)")
    sf = soundfont or os.environ.get("SOUNDFONT")
    if not sf or not Path(sf).exists():
        raise RuntimeError(
            "サウンドフォント(.sf2)を --soundfont か環境変数 SOUNDFONT で指定してください"
        )
    cmd = [fluidsynth, "-ni", "-g", "1.0", "-F", str(wav_path), "-r", "44100",
           str(sf), str(midi_path)]
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"fluidsynthが失敗しました:\n{proc.stderr[-2000:]}")
    return wav_path


def resolve_accompaniment(
    project: Project, work: Path, soundfont: str | None
) -> Path:
    """伴奏wavを用意する。

    音源プロジェクト(analyze-audio)は分離済みの伴奏wavをそのまま使い、
    MIDIプロジェクトはメロディ消音MIDIをfluidsynthでレンダリングする。
    """
    acc_path = project.song.accompaniment_path
    if acc_path:
        acc = Path(acc_path)
        if not acc.exists():
            raise RuntimeError(f"分離済み伴奏がありません({acc})")
        return acc
    acc_mid = make_accompaniment_midi(project, work / "accompaniment.mid")
    return render_midi(acc_mid, work / "accompaniment.wav", soundfont)


def mix(
    project: Project,
    project_dir: Path,
    soundfont: str | None = None,
    vocal_gain: float = 1.0,
    accompaniment_gain: float = 0.6,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg が見つかりません")
    vocal = vocal_path(project_dir)
    if not vocal.exists():
        raise RuntimeError(f"歌唱wavがありません({vocal})。先に synthesize を実行してください")

    work = project_dir / MIX_DIR
    work.mkdir(parents=True, exist_ok=True)
    acc_wav = resolve_accompaniment(project, work, soundfont)

    out = work / "song.wav"
    cmd = [
        ffmpeg, "-y",
        "-i", str(acc_wav),
        "-i", str(vocal),
        "-filter_complex",
        f"[0:a]volume={accompaniment_gain}[a0];"
        f"[1:a]volume={vocal_gain}[a1];"
        "[a0][a1]amix=inputs=2:duration=longest:normalize=0[out]",
        "-map", "[out]",
        str(out),
    ]
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpegミックスが失敗しました:\n{proc.stderr[-2000:]}")
    return out
