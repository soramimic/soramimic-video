"""Uploaded audio validation and conversion for the web API."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_AUDIO_FORMATS: dict[str, frozenset[str]] = {
    ".mp3": frozenset({"mp3"}),
    ".m4a": frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}),
    ".aac": frozenset({"aac"}),
    ".flac": frozenset({"flac"}),
    ".ogg": frozenset({"ogg"}),
    ".oga": frozenset({"ogg"}),
    ".opus": frozenset({"ogg"}),
    ".webm": frozenset({"matroska", "webm"}),
}
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", *SUPPORTED_AUDIO_FORMATS})


@dataclass(frozen=True)
class AudioInputError(ValueError):
    status_code: int
    detail: str


def audio_tools_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def validate_pcm_wav(data: bytes, maximum: int) -> float:
    """Validate the PCM WAV accepted by the analysis pipeline and return seconds."""
    if len(data) > maximum:
        raise AudioInputError(
            413,
            f"WAVファイルが大きすぎます(上限は{maximum / 1024 / 1024:.0f}MBです)",
        )
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioInputError(400, "WAVファイルではありません")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            frames = wav.getnframes()
            width = wav.getsampwidth()
    except (EOFError, wave.Error) as exc:
        raise AudioInputError(
            400,
            "PCM形式のWAVファイルを選んでください(float WAVには対応していません)",
        ) from exc
    if channels not in (1, 2) or rate <= 0 or frames <= 0 or width not in (1, 2, 3, 4):
        raise AudioInputError(400, "対応していないWAV形式です")
    return frames / rate


def _duration_ja(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}分{secs}秒" if minutes else f"{secs}秒"


def normalize_compressed_audio(
    data: bytes,
    filename: str,
    maximum: int,
    max_seconds: float,
) -> tuple[bytes, float]:
    """Verify a supported compressed audio file and convert it to mono PCM WAV."""
    if len(data) > maximum:
        raise AudioInputError(
            413,
            f"音声ファイルが大きすぎます(上限は{maximum / 1024 / 1024:.0f}MBです)",
        )
    suffix = Path(filename).suffix.lower()
    expected_formats = SUPPORTED_AUDIO_FORMATS.get(suffix)
    if expected_formats is None:
        raise AudioInputError(400, "対応していない音声形式です")
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        raise AudioInputError(503, "このサーバーでは音声入力を利用できません")

    try:
        with tempfile.TemporaryDirectory(prefix="soramimic-audio-") as raw_dir:
            source = Path(raw_dir) / f"input{suffix}"
            output = Path(raw_dir) / "input.wav"
            source.write_bytes(data)
            probe = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries",
                    "format=format_name,duration:stream=codec_type:stream_disposition=attached_pic",
                    "-of", "json", str(source),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if probe.returncode != 0:
                raise ValueError("probe failed")
            info = json.loads(probe.stdout)
            actual_formats = set(str(info.get("format", {}).get("format_name", "")).split(","))
            streams = info.get("streams", [])
            has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
            has_video = any(
                stream.get("codec_type") == "video"
                and not bool(stream.get("disposition", {}).get("attached_pic"))
                for stream in streams
            )
            if not has_audio or has_video or not actual_formats.intersection(expected_formats):
                raise ValueError("unexpected media")
            seconds = float(info.get("format", {}).get("duration", 0))
            if not (0 < seconds < float("inf")):
                raise ValueError("invalid duration")
            if max_seconds > 0 and seconds > max_seconds:
                raise AudioInputError(
                    400,
                    f"曲が長すぎます(この曲は{_duration_ja(seconds)}、"
                    f"上限は{_duration_ja(max_seconds)}です)。もっと短い曲でお試しください。",
                )

            command = [
                ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100",
                "-c:a", "pcm_s16le",
            ]
            if max_seconds > 0:
                command.extend(("-t", str(max_seconds + 1)))
            command.append(str(output))
            converted = subprocess.run(
                command, capture_output=True, timeout=120, check=False,
            )
            if converted.returncode != 0 or not output.is_file():
                raise ValueError("decode failed")
            normalized = output.read_bytes()
            return normalized, validate_pcm_wav(normalized, maximum)
    except AudioInputError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        raise AudioInputError(
            400,
            "音声ファイルを読み取れません。対応形式の音声ファイルを選んでください",
        ) from exc
