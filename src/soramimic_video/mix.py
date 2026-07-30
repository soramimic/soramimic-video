"""ミックスステージ: 元MIDIの伴奏(メロディ消音)+ NEUTRINO歌唱 → song.wav。

伴奏はメロディチャンネルのnoteイベントを除いたMIDIをfluidsynthでレンダリングする。
vocal.wav は曲頭(tick 0)からレンダリングされているので、そのまま重ねられる。
synthesizeが曲全体のキー変更(project.song.key_shift)を決めていた場合は、
伴奏も同じだけ移調して歌と調を合わせる(カラオケのキー変更。octave.py参照)。
"""

from __future__ import annotations

import json
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

# 伴奏を歌声より何dB下に置くか(歌のためのヘッドルーム)
TARGET_VOCAL_HEADROOM_DB = 2.0
# 自動計算した伴奏ゲインの上限。従来の固定値と同じで、今より持ち上げる方向にはしない
ACCOMPANIMENT_GAIN_MAX = 0.6
# 下限。下げすぎると伴奏が消えてカラオケ感がなくなる
ACCOMPANIMENT_GAIN_MIN = 0.15
# loudnorm が返す無音相当のラウドネス(これ以下は測定失敗とみなす)
SILENCE_LUFS = -70.0

# GM のリズムチャンネル。noteが音高ではなく打楽器の種類なので移調してはいけない
DRUM_CHANNEL = 9


def make_accompaniment_midi(project: Project, out_path: Path) -> Path:
    """元MIDIからメロディchのnoteを抜いた伴奏MIDIを書き出す。

    project.song.key_shift が非0なら、ドラム(ch9)以外のnoteを同じだけ移調する
    (歌の自動キー変更に伴奏を合わせる)。移調後の音高は0〜127にクランプする。
    """
    src = mido.MidiFile(project.song.midi_path, clip=True)
    melody = project.song.melody_channel
    key_shift = project.song.key_shift
    transposed = 0
    for track in src.tracks:
        removed: list = []
        tick_carry = 0
        new_msgs = []
        for msg in track:
            time = msg.time + tick_carry
            tick_carry = 0
            is_note = msg.type in ("note_on", "note_off")
            channel = getattr(msg, "channel", None)
            if is_note and channel == melody:
                tick_carry = time  # イベントを消してデルタ時間は次に繰り越す
                removed.append(msg)
                continue
            new = msg.copy(time=time)
            if key_shift and is_note and channel != DRUM_CHANNEL:
                new.note = max(0, min(127, new.note + key_shift))
                transposed += 1
            new_msgs.append(new)
        track[:] = new_msgs
        if removed:
            logger.debug("%d noteイベントをメロディch=%sから除去", len(removed), melody)
    if key_shift:
        logger.info(
            "歌のキー変更に合わせて伴奏を%+d半音移調しました(%dイベント。ドラムch%dは除く)",
            key_shift, transposed, DRUM_CHANNEL,
        )
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
        if project.song.key_shift:
            # 自動調整は伴奏wavを持つプロジェクトでキー変更を選ばない
            # (octave.resolve_auto_shift)。ここに来たらそのガードの漏れ
            logger.warning(
                "伴奏wavは移調できないため、歌のキー変更%+d半音とずれます",
                project.song.key_shift,
            )
        return acc
    acc_mid = make_accompaniment_midi(project, work / "accompaniment.mid")
    return render_midi(acc_mid, work / "accompaniment.wav", soundfont)


def measure_loudness(path: Path) -> float | None:
    """wavの integrated loudness (LUFS) を ffmpeg の loudnorm で測る。

    loudnorm はゲート付きなので、前奏・間奏の無音や小音量区間に引きずられにくい。
    測れなかった場合(ffmpeg異常・JSON解析不能・無音)は警告を出して None を返す。
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning("ffmpeg が見つからないのでラウドネス測定を省略します(%s)", path)
        return None
    cmd = [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    proc = runproc.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        logger.warning(
            "ラウドネス測定に失敗しました(%s):\n%s", path, (proc.stderr or "")[-500:]
        )
        return None
    # loudnorm のJSONは stderr の末尾に出る(フラットなオブジェクト)
    text = proc.stderr or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        logger.warning("ラウドネス測定のJSONが見つかりません(%s)", path)
        return None
    try:
        value = float(json.loads(text[start : end + 1])["input_i"])
    except (ValueError, TypeError, KeyError):
        logger.warning("ラウドネス測定のJSONを解釈できません(%s)", path)
        return None
    if value <= SILENCE_LUFS:
        logger.warning("ラウドネスが無音相当です(%s: %.1f LUFS)", path, value)
        return None
    return value


def auto_accompaniment_gain(
    vocal_lufs: float | None, accompaniment_lufs: float | None
) -> float:
    """伴奏が歌声より TARGET_VOCAL_HEADROOM_DB だけ低くなる伴奏ゲインを求める。

    どちらかが測れていなければ従来の固定値(=上限)にフォールバックする。
    求めたゲインは ACCOMPANIMENT_GAIN_MIN〜MAX にクリップする。
    """
    if vocal_lufs is None or accompaniment_lufs is None:
        logger.warning(
            "ラウドネスを測れなかったので伴奏ゲインは固定値 %.2f を使います",
            ACCOMPANIMENT_GAIN_MAX,
        )
        return ACCOMPANIMENT_GAIN_MAX
    gain_db = (vocal_lufs - TARGET_VOCAL_HEADROOM_DB) - accompaniment_lufs
    gain = 10 ** (gain_db / 20)
    clipped = min(max(gain, ACCOMPANIMENT_GAIN_MIN), ACCOMPANIMENT_GAIN_MAX)
    if clipped != gain:
        logger.info(
            "伴奏ゲインをクリップしました(歌声 %.1f LUFS / 伴奏 %.1f LUFS → "
            "計算値 %.3f, 採用 %.3f)",
            vocal_lufs, accompaniment_lufs, gain, clipped,
        )
    else:
        logger.info(
            "伴奏ゲインを自動決定しました(歌声 %.1f LUFS / 伴奏 %.1f LUFS → %.3f)",
            vocal_lufs, accompaniment_lufs, clipped,
        )
    return clipped


def mix(
    project: Project,
    project_dir: Path,
    soundfont: str | None = None,
    vocal_gain: float = 1.0,
    accompaniment_gain: float | None = None,
) -> Path:
    """歌唱wavと伴奏wavを重ねて song.wav を作る。

    accompaniment_gain を省略すると、歌声と伴奏のラウドネスを測って
    歌が埋もれないゲインを自動計算する(auto_accompaniment_gain)。
    明示指定した場合はその値をそのまま使う。
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg が見つかりません")
    vocal = vocal_path(project_dir)
    if not vocal.exists():
        raise RuntimeError(f"歌唱wavがありません({vocal})。先に synthesize を実行してください")

    work = project_dir / MIX_DIR
    work.mkdir(parents=True, exist_ok=True)
    acc_wav = resolve_accompaniment(project, work, soundfont)
    if accompaniment_gain is None:
        accompaniment_gain = auto_accompaniment_gain(
            measure_loudness(vocal), measure_loudness(acc_wav)
        )

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
