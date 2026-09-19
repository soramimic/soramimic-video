from __future__ import annotations

import hashlib
import io
import json
import wave

import pytest

from helpers import build_xf_midi
from soramimic_video.sample_validation import validate_sample_directory


def _sample(tmp_path, sample_id: str = "local"):
    (tmp_path / "samples.local.json").write_text(
        json.dumps([{"id": sample_id, "title": "検査曲"}]), encoding="utf-8"
    )
    build_xf_midi(
        tmp_path / f"{sample_id}.mid",
        notes=[(0, 240, 60), (480, 240, 62)],
        lyric_events=[(0, "ラ"), (480, "/ラ")],
    )
    (tmp_path / f"{sample_id}_lyrics.txt").write_text("ラ\nラ\n", encoding="utf-8")


def test_validates_local_xf_samples(tmp_path):
    _sample(tmp_path)

    result = validate_sample_directory(tmp_path, local_only=True)[0]

    assert (result.sample_id, result.notes, result.lines, result.matched_lines) == (
        "local",
        2,
        2,
        2,
    )


def test_rejects_missing_sample_asset(tmp_path):
    _sample(tmp_path)
    (tmp_path / "local_lyrics.txt").unlink()

    with pytest.raises(ValueError, match="ファイルがありません"):
        validate_sample_directory(tmp_path, local_only=True)


def test_rejects_unmatched_original_lyrics(tmp_path):
    _sample(tmp_path)
    (tmp_path / "local_lyrics.txt").write_text("全然違う歌詞\n", encoding="utf-8")

    with pytest.raises(ValueError, match="対応しないXF行"):
        validate_sample_directory(tmp_path, local_only=True)


def test_validates_audio_samples_and_hash(tmp_path):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 800)
    data = output.getvalue()
    (tmp_path / "samples.json").write_text("[]", encoding="utf-8")
    (tmp_path / "audio_samples.json").write_text(
        json.dumps(
            [{
                "id": "voice",
                "input_kind": "audio",
                "sha256": hashlib.sha256(data).hexdigest(),
            }]
        ),
        encoding="utf-8",
    )
    (tmp_path / "voice.wav").write_bytes(data)
    (tmp_path / "voice_lyrics.txt").write_text("あ\n", encoding="utf-8")

    result = validate_sample_directory(tmp_path)[0]

    assert result.input_kind == "audio"
    assert result.seconds == pytest.approx(0.1)
