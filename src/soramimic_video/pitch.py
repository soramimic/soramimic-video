"""vocals.wav の f0 抽出とモーラごとの音高(midi_note)決定。

f0 は librosa.pyin。モーラの音高は区間内の有声フレームの最頻半音を基準に、
WAV由来のF0特徴だけを使う軽量補正器で高確度な誤りだけを直す
(v1 は 1モーラ=1音符。モーラ内の音程変化=メリスマは扱わない)。
有声区間の情報は、CTCアライメントのスパイク状スパンを実際の歌唱長へ
伸長するのにも使う(voiced_end)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .pitch_corrector import correct_midi_notes

logger = logging.getLogger(__name__)

SAMPLING_RATE_PITCH = 16000
FMIN_HZ = 65.0  # C2付近
FMAX_HZ = 1100.0  # C6付近
MIDI_MIN = 36
MIDI_MAX = 96
_UNVOICED_BREAK_FRAMES = 3  # これ以上連続で無声なら歌唱が途切れたとみなす


@dataclass
class PitchTrack:
    times: np.ndarray  # フレーム時刻(秒)
    midi: np.ndarray  # midiノート値(float)。無声はNaN
    voiced_probability: np.ndarray | None = None
    short_window: PitchTrack | None = None

    def frame_period(self) -> float:
        return float(self.times[1] - self.times[0]) if len(self.times) > 1 else 0.032


def extract_pitch(vocals_path: Path) -> PitchTrack:
    import librosa

    logger.info("f0抽出中(pyin)...")
    y, sr = librosa.load(str(vocals_path), sr=SAMPLING_RATE_PITCH, mono=True)
    f0, _, voiced_probability = librosa.pyin(
        y,
        fmin=FMIN_HZ,
        fmax=FMAX_HZ,
        sr=sr,
        frame_length=2048,
        hop_length=512,
    )
    short_f0, _, short_probability = librosa.pyin(
        y,
        fmin=FMIN_HZ,
        fmax=FMAX_HZ,
        sr=sr,
        frame_length=1024,
        hop_length=160,
    )
    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=512)
    short_times = librosa.frames_to_time(
        np.arange(len(short_f0)), sr=sr, hop_length=160
    )
    midi = librosa.hz_to_midi(f0)  # NaNは無声のまま
    short_midi = librosa.hz_to_midi(short_f0)
    return PitchTrack(
        times=times,
        midi=midi,
        voiced_probability=voiced_probability,
        short_window=PitchTrack(
            times=short_times,
            midi=short_midi,
            voiced_probability=short_probability,
        ),
    )


def mora_midi_notes(
    track: PitchTrack, spans: list[tuple[float, float]], default: int = 60
) -> list[int]:
    """各モーラ区間の midi_note を決める。

    区間内の有声フレームの最頻半音(モード)。中央値はビブラート・しゃくり・
    リリースのピッチdrift に引っ張られて隣の半音へ外れやすいが、モードは
    最も長く保持された半音を拾うため頑健(XF正解評価: RMVPEのf0で
    中央値79%→モード86%)。無ければ少し広げて再試行し、それでも無ければ
    直前のモーラの音高(先頭なら後続、全滅なら default)。
    """
    raw: list[int | None] = []
    for start, end in spans:
        value = _mode_in_range(track, start, end)
        if value is None:
            value = _mode_in_range(track, start - 0.05, end + 0.05)
        raw.append(value)

    notes: list[int] = []
    for i, value in enumerate(raw):
        if value is None:
            value = next((v for v in reversed(raw[:i]) if v is not None), None)
        if value is None:
            value = next((v for v in raw[i + 1 :] if v is not None), None)
        if value is None:
            value = default
        notes.append(int(value))
    if track.short_window is not None:
        notes = correct_midi_notes(track, track.short_window, spans, notes)
    return [int(np.clip(note, MIDI_MIN, MIDI_MAX)) for note in notes]


def _mode_in_range(track: PitchTrack, start: float, end: float) -> int | None:
    """区間内の有声フレームを半音に丸めた最頻値。同数なら中央値に近い方。"""
    sel = (track.times >= start) & (track.times < end)
    values = track.midi[sel]
    values = values[~np.isnan(values)]
    if len(values) == 0:
        return None
    semis = np.round(values).astype(int)
    counts = np.bincount(semis - semis.min())
    top = int(counts.max())
    candidates = [semis.min() + k for k, c in enumerate(counts) if c == top]
    if len(candidates) == 1:
        return candidates[0]
    med = float(np.median(values))
    return min(candidates, key=lambda s: abs(s - med))


def voiced_end(track: PitchTrack, start_sec: float, limit_sec: float) -> float:
    """start_sec から歌唱(有声)が続く終端を返す(limit_secでキャップ)。

    _UNVOICED_BREAK_FRAMES 以上連続で無声になった時点で途切れたとみなす。
    """
    if limit_sec <= start_sec:
        return start_sec
    sel = np.where((track.times >= start_sec) & (track.times < limit_sec))[0]
    if len(sel) == 0:
        return limit_sec
    unvoiced_run = 0
    for idx in sel:
        if np.isnan(track.midi[idx]):
            unvoiced_run += 1
            if unvoiced_run >= _UNVOICED_BREAK_FRAMES:
                return float(track.times[idx - unvoiced_run + 1])
        else:
            unvoiced_run = 0
    return limit_sec
