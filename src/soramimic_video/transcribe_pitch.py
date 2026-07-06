"""歌唱ノート採譜(RMVPE + ROSVOT)のラッパー(issue #5)。

外部MIDIが無い曲(Suno生成等)向けに、ボーカルからノート列(onset/offset/pitch)を
起こして pseudo-MIDI とし、melody_align に流す。ROSVOTは同梱しない
(https://github.com/RickyL-2000/ROSVOT)。macOSではCPU/MPS用の小さなパッチが要る
(inference/rosvot.py のデバイス固定を外す)ので、パッチ済みのクローンを
環境変数 ROSVOT_ROOT で指定する。ROSVOTは重い依存を持つため、必要なら別の
Python環境を ROSVOT_PYTHON で指定できる(既定は soramimic-video と同じ環境)。

出力の .mid は通常のSMFなので melody_align.load_midi_notes(mido)で読める。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .melody_align import MelodyNote, load_midi_notes

logger = logging.getLogger(__name__)

TRANSCRIBE_DIR = "transcribe"


def rosvot_root() -> Path:
    root = os.environ.get("ROSVOT_ROOT")
    if not root:
        raise RuntimeError(
            "環境変数 ROSVOT_ROOT が設定されていません。"
            "ROSVOT(https://github.com/RickyL-2000/ROSVOT)を取得し、"
            "macOSではCPU/MPS用パッチを当てた上でルートを指定してください"
        )
    path = Path(root).expanduser()
    if not (path / "inference" / "rosvot.py").exists():
        raise RuntimeError(
            f"ROSVOT_ROOT が不正です(inference/rosvot.py がありません): {path}"
        )
    return path


def transcribe_notes(
    vocals_path: Path,
    project_dir: Path,
    device: str | None = None,
) -> list[MelodyNote]:
    """ボーカルwavをROSVOTで採譜し、ノート列(音源時間軸)を返す。"""
    root = rosvot_root()
    python = os.environ.get("ROSVOT_PYTHON", sys.executable)
    # ROSVOTは cwd=root で動かすので、入出力は絶対パスで渡す
    vocals_abs = vocals_path.resolve()
    out_dir = (project_dir / TRANSCRIBE_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_mid = out_dir / "midi" / "output.mid"
    if out_mid.exists():
        logger.info("採譜済みの出力を再利用: %s", out_mid)
        return _load_notes(out_mid)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["ROSVOT_DEVICE"] = device or _default_device()
    cmd = [python, "inference/rosvot.py", "-o", str(out_dir), "-p", str(vocals_abs)]
    logger.info("ROSVOTで歌唱を採譜中(数十秒): %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=str(root), env=env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 or not out_mid.exists():
        raise RuntimeError(f"ROSVOTが失敗しました:\n{proc.stderr[-2000:]}")
    return _load_notes(out_mid)


def _load_notes(midi_path: Path) -> list[MelodyNote]:
    by_channel = load_midi_notes(midi_path)
    notes = [n for ch_notes in by_channel.values() for n in ch_notes]
    notes.sort(key=lambda n: n.start_sec)
    logger.info("採譜結果: %d音", len(notes))
    return notes


def _default_device() -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    return "mps" if torch.backends.mps.is_available() else "cpu"
