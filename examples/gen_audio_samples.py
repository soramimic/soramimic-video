#!/usr/bin/env python3
"""Generate the bundled WAV analysis samples from project-owned MIDI files.

The generated voice uses HTS Voice "Mei", distributed with pyopenjtalk under
CC BY 3.0. The accompaniment contains no third-party completed song or
commercial recording; it renders this repository's MIDI with FluidR3 GM under
the MIT license.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import pyopenjtalk

from soramimic_video.mix import make_accompaniment_midi
from soramimic_video.xfparse import analyze_midi

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "src" / "soramimic_video" / "static" / "sample"
DEFAULT_IDS = ("katatsumuri", "momotarou", "harugakita")
RATE = 48_000


def _trim(signal: np.ndarray, threshold: float = 0.002) -> np.ndarray:
    positions = np.flatnonzero(np.abs(signal) >= threshold)
    if not len(positions):
        return signal
    margin = int(0.025 * RATE)
    return signal[max(0, positions[0] - margin) : min(len(signal), positions[-1] + margin)]


def _fade(signal: np.ndarray, seconds: float = 0.015) -> np.ndarray:
    size = min(int(seconds * RATE), len(signal) // 2)
    if size:
        signal[:size] *= np.linspace(0.0, 1.0, size)
        signal[-size:] *= np.linspace(1.0, 0.0, size)
    return signal


def _tts_to_window(text: str, seconds: float, half_tone: float = 0.0) -> np.ndarray:
    # Prefer changing the synthesizer's speaking rate over resampling: resampling
    # would alter the pitch that the audio analyzer is intended to recover.
    speed = 1.0
    signal = np.zeros(1)
    for speed in (1.0, 1.15, 1.35, 1.6, 1.9, 2.2):
        signal, rate = pyopenjtalk.tts(text, speed=speed, half_tone=half_tone)
        if rate != RATE:
            raise RuntimeError(f"unexpected pyopenjtalk rate: {rate}")
        signal = _trim(np.asarray(signal, dtype=np.float64))
        if len(signal) <= int(seconds * RATE):
            break
    return _fade(signal[: int(seconds * RATE)].copy())


def _voice_phrase(project) -> np.ndarray:
    duration = max(note.end_sec for note in project.notes) + 0.5
    output = np.zeros(int(duration * RATE))
    for line in project.lines:
        notes = [project.note_by_id(note_id) for note_id in line.note_ids]
        start = notes[0].start_sec
        end = notes[-1].end_sec
        text = line.original_text or line.xf_surface
        voice = _tts_to_window(text, max(0.2, end - start - 0.04))
        begin = int(start * RATE)
        output[begin : begin + len(voice)] += voice
    return output


def _voice_notes(project) -> np.ndarray:
    duration = max(note.end_sec for note in project.notes) + 0.5
    output = np.zeros(int(duration * RATE))
    # Mei's unshifted voice is near A3.  Per-note shifts make this candidate
    # melody-like while retaining the phonemes produced by Open JTalk.
    reference_midi = 57
    for note in project.notes:
        window = max(0.08, note.end_sec - note.start_sec - 0.015)
        voice = _tts_to_window(note.kana, window, note.midi_note - reference_midi)
        begin = int(note.start_sec * RATE)
        output[begin : begin + len(voice)] += voice
    return output


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        if width != 2:
            raise RuntimeError(f"expected PCM16 accompaniment, got {width * 8}-bit")
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float64)
    return data.reshape(-1, channels).mean(axis=1) / 32768.0, rate


def _resample(signal: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == RATE:
        return signal
    size = round(len(signal) * RATE / source_rate)
    return np.interp(
        np.linspace(0, len(signal), size, endpoint=False),
        np.arange(len(signal)),
        signal,
    )


def _write_wav(path: Path, signal: np.ndarray) -> None:
    peak = float(np.max(np.abs(signal))) or 1.0
    signal = np.clip(signal * min(0.94 / peak, 1.0), -1.0, 1.0)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((signal * 32767).astype("<i2").tobytes())


def generate(sample_id: str, mode: str, output: Path, soundfont: Path) -> None:
    midi = SAMPLES / f"{sample_id}.mid"
    lyrics = SAMPLES / f"{sample_id}_lyrics.txt"
    project = analyze_midi(midi)
    original_lines = lyrics.read_text(encoding="utf-8").splitlines()
    for line, original in zip(project.lines, original_lines, strict=True):
        line.original_text = original
    voice = _voice_phrase(project) if mode == "phrase" else _voice_notes(project)
    with tempfile.TemporaryDirectory(prefix="soramimic-audio-sample-") as raw_tmp:
        temp = Path(raw_tmp)
        accompaniment_midi = make_accompaniment_midi(project, temp / "accompaniment.mid")
        accompaniment_wav = temp / "accompaniment.wav"
        subprocess.run(
            [
                "fluidsynth",
                "-ni",
                "-g",
                "0.35",
                "-F",
                str(accompaniment_wav),
                "-r",
                "44100",
                str(soundfont),
                str(accompaniment_midi),
            ],
            check=True,
            capture_output=True,
        )
        accompaniment, rate = _read_wav(accompaniment_wav)
    accompaniment = _resample(accompaniment, rate)
    size = max(len(voice), len(accompaniment))
    mixed = np.zeros(size)
    mixed[: len(accompaniment)] += accompaniment * 0.3
    mixed[: len(voice)] += voice * 0.9
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(output, mixed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*", default=DEFAULT_IDS)
    parser.add_argument("--mode", choices=("phrase", "notes"), default="phrase")
    parser.add_argument("--output-dir", type=Path, default=SAMPLES)
    parser.add_argument(
        "--soundfont", type=Path, default=Path("/usr/share/sounds/sf2/FluidR3_GM.sf2")
    )
    args = parser.parse_args()
    for sample_id in args.ids:
        output = args.output_dir / f"{sample_id}_audio.wav"
        generate(sample_id, args.mode, output, args.soundfont)
        print(output)


if __name__ == "__main__":
    main()
