"""音源分離ステージ: demucs で歌唱音源をボーカル/伴奏に分ける。

vocals.wav は ASR・アライメント・ピッチ抽出の入力、
no_vocals.wav は mix ステージの伴奏(fluidsynthレンダリングの代わり)になる。
分離は遅いので、出力が既にあればスキップする。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEMUCS_MODEL = "htdemucs"


def separate(audio_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """audio_path を分離して (vocals.wav, no_vocals.wav) のパスを返す。"""
    vocals = out_dir / "vocals.wav"
    accompaniment = out_dir / "no_vocals.wav"
    if vocals.exists() and accompaniment.exists():
        logger.info("分離済みの出力を再利用: %s", out_dir)
        return vocals, accompaniment

    if importlib.util.find_spec("demucs") is None:
        raise RuntimeError(
            "demucs がインストールされていません(uv sync --extra audio)"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "demucs"
    cmd = [
        sys.executable, "-m", "demucs.separate",
        "--two-stems", "vocals",
        "-n", DEMUCS_MODEL,
        "-o", str(work),
        str(audio_path),
    ]
    logger.info("demucs実行中(数分かかります): %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"demucsが失敗しました:\n{proc.stderr[-2000:]}")

    stem_dir = work / DEMUCS_MODEL / audio_path.stem
    for src, dst in [(stem_dir / "vocals.wav", vocals),
                     (stem_dir / "no_vocals.wav", accompaniment)]:
        if not src.exists():
            raise RuntimeError(f"demucsの出力が見つかりません: {src}")
        shutil.move(str(src), str(dst))
    shutil.rmtree(work, ignore_errors=True)
    return vocals, accompaniment
