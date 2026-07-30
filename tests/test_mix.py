import shutil
import subprocess
from pathlib import Path

import mido
import pytest

from helpers import build_xf_midi
from soramimic_video import runproc
from soramimic_video.mix import (
    ACCOMPANIMENT_GAIN_MAX,
    ACCOMPANIMENT_GAIN_MIN,
    auto_accompaniment_gain,
    make_accompaniment_midi,
    measure_loudness,
    mix,
    resolve_accompaniment,
)
from soramimic_video.project import Project, SongInfo
from soramimic_video.synthesize import vocal_path
from soramimic_video.xfparse import analyze_midi

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_make_accompaniment_midi_removes_melody(tmp_path: Path):
    midi_path = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 480, 60), (480, 480, 62)],
        lyric_events=[(0, "あ"), (480, "い")],
    )
    # 伴奏チャンネル(ch1)の音を後から足す
    mid = mido.MidiFile(str(midi_path), clip=True)
    data = midi_path.read_bytes()
    xf_start = data.index(b"XFIH")
    track = mid.tracks[0]
    eot = track.pop()  # end_of_track
    track.append(mido.Message("note_on", channel=1, note=40, velocity=80, time=0))
    track.append(mido.Message("note_off", channel=1, note=40, velocity=64, time=480))
    track.append(eot)
    import io

    buf = io.BytesIO()
    mid.save(file=buf)
    midi_path.write_bytes(buf.getvalue() + data[xf_start:])

    project = analyze_midi(midi_path)
    assert project.song.melody_channel == 0

    out = make_accompaniment_midi(project, tmp_path / "acc.mid")
    acc = mido.MidiFile(str(out), clip=True)
    notes = [m for t in acc.tracks for m in t if m.type in ("note_on", "note_off")]
    assert notes, "伴奏の音符が残っていない"
    assert all(m.channel == 1 for m in notes)
    # デルタ時間の繰り越しでタイミングが保たれている
    total_acc = sum(m.time for m in acc.tracks[0])
    total_src = sum(m.time for m in mido.MidiFile(str(midi_path), clip=True).tracks[0])
    assert total_acc == total_src


def _midi_with_backing(path: Path, backing: list[tuple[int, int, int]]) -> Path:
    """メロディ(ch0)+伴奏イベントを持つXF MIDIを作る。backing=[(ch, note, tick長)]。"""
    midi_path = build_xf_midi(
        path, notes=[(0, 480, 60)], lyric_events=[(0, "あ")]
    )
    mid = mido.MidiFile(str(midi_path), clip=True)
    data = midi_path.read_bytes()
    xf_start = data.index(b"XFIH")
    track = mid.tracks[0]
    eot = track.pop()
    for ch, note, length in backing:
        track.append(mido.Message("note_on", channel=ch, note=note, velocity=80, time=0))
        track.append(
            mido.Message("note_off", channel=ch, note=note, velocity=64, time=length)
        )
    track.append(eot)
    import io

    buf = io.BytesIO()
    mid.save(file=buf)
    midi_path.write_bytes(buf.getvalue() + data[xf_start:])
    return midi_path


def _accompaniment_notes(project, tmp_path: Path, name: str) -> list:
    out = make_accompaniment_midi(project, tmp_path / name)
    acc = mido.MidiFile(str(out), clip=True)
    return [m for t in acc.tracks for m in t if m.type in ("note_on", "note_off")]


def test_make_accompaniment_midi_transposes_by_key_shift(tmp_path: Path):
    """自動キー変更ぶんだけ伴奏を移調する(ドラムch9は移調しない)。"""
    midi_path = _midi_with_backing(
        tmp_path / "song.mid", [(1, 40, 480), (9, 36, 480), (2, 55, 480)]
    )
    project = analyze_midi(midi_path)
    project.song.key_shift = -5

    notes = _accompaniment_notes(project, tmp_path, "acc.mid")
    by_channel = {(m.channel, m.note) for m in notes}
    assert (1, 35) in by_channel  # 40 - 5
    assert (2, 50) in by_channel  # 55 - 5
    assert (9, 36) in by_channel  # ドラムは打楽器の種類なので不変


def test_make_accompaniment_midi_no_transpose_by_default(tmp_path: Path):
    """key_shift=0(オクターブだけで収まる従来の曲)では伴奏は原調のまま。"""
    midi_path = _midi_with_backing(tmp_path / "song.mid", [(1, 40, 480)])
    project = analyze_midi(midi_path)
    assert project.song.key_shift == 0

    notes = _accompaniment_notes(project, tmp_path, "acc.mid")
    assert {(m.channel, m.note) for m in notes} == {(1, 40)}


def test_make_accompaniment_midi_clamps_transposed_notes(tmp_path: Path):
    """移調で0〜127を外れる音はクランプする(mido の値域エラーを避ける)。"""
    midi_path = _midi_with_backing(
        tmp_path / "song.mid", [(1, 125, 480), (2, 2, 480)]
    )
    project = analyze_midi(midi_path)
    project.song.key_shift = 6
    high = {m.note for m in _accompaniment_notes(project, tmp_path, "up.mid")}
    assert high == {127, 8}

    project.song.key_shift = -6
    low = {m.note for m in _accompaniment_notes(project, tmp_path, "down.mid")}
    assert low == {119, 0}


def test_resolve_accompaniment_uses_separated_wav(tmp_path: Path):
    """音源プロジェクトでは分離済み伴奏wavをそのまま使う(fluidsynth不要)。"""
    acc = tmp_path / "no_vocals.wav"
    acc.write_bytes(b"RIFF")
    project = Project(
        song=SongInfo(
            midi_path="", ticks_per_beat=480, accompaniment_path=str(acc)
        )
    )
    assert resolve_accompaniment(project, tmp_path, soundfont=None) == acc


def test_resolve_accompaniment_missing_wav_raises(tmp_path: Path):
    project = Project(
        song=SongInfo(
            midi_path="",
            ticks_per_beat=480,
            accompaniment_path=str(tmp_path / "nai.wav"),
        )
    )
    with pytest.raises(RuntimeError, match="伴奏"):
        resolve_accompaniment(project, tmp_path, soundfont=None)


# --- ラウドネス自動バランス ---


def make_tone(path: Path, volume_db: float, seconds: float = 6.0) -> Path:
    """指定ラウドネス相当のサイン波wavを作る(テスト用の擬似音源)。"""
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-hide_banner", "-nostats", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-af", f"volume={volume_db}dB", str(path)],
        check=True,
    )
    return path


def test_auto_gain_lowers_when_accompaniment_is_loud():
    """うっせぇわ相当(伴奏-13.6/歌-25.7)では伴奏を大きく絞る。"""
    gain = auto_accompaniment_gain(-25.7, -13.6)
    assert gain < ACCOMPANIMENT_GAIN_MAX
    # (-25.7 - 2.0) - (-13.6) = -14.1dB → 10**(-14.1/20)
    assert gain == pytest.approx(10 ** (-14.1 / 20), rel=1e-6)


def test_auto_gain_clips_to_max_when_levels_are_close():
    """同程度の音量なら計算値が上限を超えるので0.6に張り付く。"""
    assert auto_accompaniment_gain(-20.0, -20.0) == ACCOMPANIMENT_GAIN_MAX
    assert auto_accompaniment_gain(-10.0, -30.0) == ACCOMPANIMENT_GAIN_MAX


def test_auto_gain_keeps_lemon_close_to_current_default():
    """Lemon相当(伴奏-17.6/歌-20.7)は従来の0.6から大きく動かない。"""
    gain = auto_accompaniment_gain(-20.7, -17.6)
    assert 0.5 < gain <= ACCOMPANIMENT_GAIN_MAX


def test_auto_gain_clips_to_min_when_vocal_is_tiny():
    assert auto_accompaniment_gain(-60.0, -10.0) == ACCOMPANIMENT_GAIN_MIN


def test_auto_gain_falls_back_when_measurement_missing():
    assert auto_accompaniment_gain(None, -13.6) == ACCOMPANIMENT_GAIN_MAX
    assert auto_accompaniment_gain(-25.7, None) == ACCOMPANIMENT_GAIN_MAX
    assert auto_accompaniment_gain(None, None) == ACCOMPANIMENT_GAIN_MAX


def test_measure_loudness_invalid_file_returns_none(tmp_path: Path):
    """音声として読めないファイルは測定失敗(None)。"""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF-not-an-audio-file")
    assert measure_loudness(bad) is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_measure_loudness_reflects_volume(tmp_path: Path):
    loud = measure_loudness(make_tone(tmp_path / "loud.wav", -6))
    quiet = measure_loudness(make_tone(tmp_path / "quiet.wav", -26))
    assert loud is not None and quiet is not None
    assert loud - quiet == pytest.approx(20.0, abs=1.0)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_measure_loudness_silence_returns_none(tmp_path: Path):
    silent = tmp_path / "silent.wav"
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "6", str(silent)],
        check=True,
    )
    assert measure_loudness(silent) is None


def _mix_with_recorded_gain(
    tmp_path: Path, monkeypatch, vocal_db: float, acc_db: float, **kwargs
) -> float:
    """歌唱/伴奏wavを作って mix() を回し、実際に使われた伴奏ゲインを取り出す。"""
    acc = make_tone(tmp_path / "acc.wav", acc_db)
    vocal = vocal_path(tmp_path)
    vocal.parent.mkdir(parents=True, exist_ok=True)
    make_tone(vocal, vocal_db)
    project = Project(
        song=SongInfo(midi_path="", ticks_per_beat=480, accompaniment_path=str(acc))
    )

    real_run = runproc.run
    cmds: list[list[str]] = []

    def spy(cmd, *args, **kw):
        cmds.append(list(cmd))
        return real_run(cmd, *args, **kw)

    monkeypatch.setattr(runproc, "run", spy)
    mix(project, tmp_path, **kwargs)

    filters = [c[c.index("-filter_complex") + 1] for c in cmds if "-filter_complex" in c]
    assert len(filters) == 1
    head = filters[0].split("[a0]")[0]  # "[0:a]volume=<伴奏ゲイン>"
    return float(head.split("volume=")[1])


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_mix_auto_lowers_gain_for_quiet_vocal(tmp_path: Path, monkeypatch):
    gain = _mix_with_recorded_gain(tmp_path, monkeypatch, vocal_db=-30, acc_db=-8)
    assert ACCOMPANIMENT_GAIN_MIN <= gain < ACCOMPANIMENT_GAIN_MAX


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_mix_auto_clips_to_max_for_similar_levels(tmp_path: Path, monkeypatch):
    gain = _mix_with_recorded_gain(tmp_path, monkeypatch, vocal_db=-12, acc_db=-12)
    assert gain == ACCOMPANIMENT_GAIN_MAX


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_mix_respects_explicit_gain(tmp_path: Path, monkeypatch):
    """明示指定された伴奏ゲインは自動計算で上書きしない。"""
    gain = _mix_with_recorded_gain(
        tmp_path, monkeypatch, vocal_db=-30, acc_db=-8, accompaniment_gain=0.42
    )
    assert gain == pytest.approx(0.42)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpegがない")
def test_mix_falls_back_when_measurement_fails(tmp_path: Path, monkeypatch):
    """測定できない伴奏(壊れたwav)でも従来の固定ゲインでミックスを続ける。"""
    monkeypatch.setattr(
        "soramimic_video.mix.measure_loudness", lambda path: None
    )
    gain = _mix_with_recorded_gain(tmp_path, monkeypatch, vocal_db=-30, acc_db=-8)
    assert gain == ACCOMPANIMENT_GAIN_MAX
