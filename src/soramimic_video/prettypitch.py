"""PrettyPitch歌声合成バックエンド。

PrettyPitch本体・LeapSinger・モデルは同梱しない。開発環境で別途用意した
PrettyPitch checkoutを ``PRETTYPITCH_ROOT`` で指定し、そのcheckoutの
``svs.render`` を独立したPythonプロセスとして呼び出す。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from . import runproc
from .project import Project

logger = logging.getLogger(__name__)

ROOT_ENV = "PRETTYPITCH_ROOT"
PYTHON_ENV = "PRETTYPITCH_PYTHON"
LEAPSINGER_ROOT_ENV = "PRETTYPITCH_LEAPSINGER_ROOT"
DEVICE_ENV = "PRETTYPITCH_DEVICE"
SPEAKER_ID_ENV = "PRETTYPITCH_SPEAKER_ID"

DEFAULT_DEVICE = "cuda"
DEFAULT_SPEAKER_ID = 2  # 波音リツ
UST_TEMPO_BPM = 120.0
UST_TICKS_PER_BEAT = 480

# PrettyPitch 0.1.0の日本語表に無い、実際の替え歌生成で現れる無声母音表記。
# 外部checkoutを書き換えず、ジョブごとの派生表へ追記する。
_MORA_ADDITIONS = (
    "リュ ry U",
    "リョ ry O",
    "ヴィ v I",
)


def configured_root(value: str | Path | None = None) -> Path | None:
    raw = str(value or os.environ.get(ROOT_ENV, "")).strip()
    return Path(raw).expanduser().resolve() if raw else None


def configured_python(root: Path, value: str | Path | None = None) -> Path:
    raw = str(value or os.environ.get(PYTHON_ENV, "")).strip()
    if raw:
        # venv/bin/python は通常システムPythonへのsymlink。resolve()するとvenvの
        # sys.prefixが失われ、Torch等の環境内パッケージを読めなくなるため保持する。
        return Path(raw).expanduser().absolute()
    candidate = root / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable).resolve()


def configured_leapsinger_root(root: Path, value: str | Path | None = None) -> Path:
    raw = str(value or os.environ.get(LEAPSINGER_ROOT_ENV, "")).strip()
    return Path(raw).expanduser().resolve() if raw else root.parent / "LeapSinger"


def configured_device(value: str | None = None) -> str:
    return str(value or os.environ.get(DEVICE_ENV, DEFAULT_DEVICE)).strip() or DEFAULT_DEVICE


def configured_speaker_id(value: int | str | None = None) -> int:
    raw = value if value is not None else os.environ.get(SPEAKER_ID_ENV, DEFAULT_SPEAKER_ID)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{SPEAKER_ID_ENV}は整数で指定してください: {raw!r}") from exc


def installation_error(
    root: str | Path | None = None,
    python: str | Path | None = None,
    leapsinger_root: str | Path | None = None,
) -> str | None:
    """設定済みランタイムを検査し、利用可能ならNoneを返す。"""
    resolved_root = configured_root(root)
    if resolved_root is None:
        return f"{ROOT_ENV}が設定されていません"
    required = (
        resolved_root / "svs" / "render.py",
        resolved_root / "dict" / "ja.mora",
        resolved_root / "checkpoints" / "f0_3singer.pt",
        resolved_root / "checkpoints" / "cons_dur_3singer.pt",
        resolved_root / "checkpoints" / "nhv_v3_2_1.onnx",
    )
    missing = next((path for path in required if not path.is_file()), None)
    if missing is not None:
        return f"PrettyPitchの必要ファイルがありません: {missing}"
    resolved_python = configured_python(resolved_root, python)
    if not resolved_python.is_file() or not os.access(resolved_python, os.X_OK):
        return f"PrettyPitch用Pythonが実行できません: {resolved_python}"
    resolved_leapsinger = configured_leapsinger_root(resolved_root, leapsinger_root)
    if not (resolved_leapsinger / "infer.py").is_file():
        return f"LeapSingerの推論コードがありません: {resolved_leapsinger}"
    acoustic_roots = (
        resolved_root / "checkpoints" / "leapsinger",
        resolved_leapsinger / "checkpoints",
    )
    if not any(
        next(path.rglob("3speaker_gan2d.pth"), None) is not None
        for path in acoustic_roots
        if path.is_dir()
    ):
        return "LeapSingerの音響モデル(3speaker_gan2d.pth)がありません"
    return None


def available() -> bool:
    return installation_error() is None


def _ust_section(index: int, length: int, lyric: str, note_num: int) -> str:
    return (
        f"[#{index:04d}]\n"
        f"Length={length}\n"
        f"Lyric={lyric}\n"
        f"NoteNum={note_num}\n"
    )


def build_ust(project: Project, lyric_map: dict[int, str], transpose: int = 0) -> str:
    """絶対秒を保つ固定120 BPMのUSTを作る。

    Projectのtempo mapに依存せず ``start_sec`` / ``end_sec`` を直接tickへ写すため、
    音源解析由来の可変テンポ曲でもPrettyPitch側の単一テンポ制約を回避できる。
    """
    ticks_per_second = UST_TICKS_PER_BEAT * UST_TEMPO_BPM / 60.0
    chunks = [
        "[#VERSION]\nUST Version1.2\n",
        f"[#SETTING]\nTempo={UST_TEMPO_BPM:.9f}\n",
    ]
    cursor = 0
    index = 0
    for note in sorted(project.notes, key=lambda item: (item.start_sec, item.id)):
        start = max(cursor, round(float(note.start_sec) * ticks_per_second))
        end = max(start + 1, round(float(note.end_sec) * ticks_per_second))
        if start > cursor:
            chunks.append(_ust_section(index, start - cursor, "R", 60))
            index += 1
        lyric = str(lyric_map.get(note.id) or note.kana or "ア").strip() or "ア"
        chunks.append(
            _ust_section(index, end - start, lyric, int(note.midi_note) + int(transpose))
        )
        index += 1
        cursor = end
    chunks.append("[#TRACKEND]\n")
    return "".join(chunks)


def _write_mora_table(root: Path, destination: Path) -> None:
    base = (root / "dict" / "ja.mora").read_text(encoding="utf-8")
    present = {
        line.split(maxsplit=1)[0]
        for line in base.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    additions = [line for line in _MORA_ADDITIONS if line.split(maxsplit=1)[0] not in present]
    destination.write_text(
        base.rstrip() + "\n\n# Soramimic runtime aliases\n" + "\n".join(additions) + "\n",
        encoding="utf-8",
    )


def run_prettypitch(
    project: Project,
    project_dir: Path,
    *,
    lyric_map: dict[int, str],
    root: str | Path | None = None,
    python: str | Path | None = None,
    leapsinger_root: str | Path | None = None,
    speaker_id: int | str | None = None,
    device: str | None = None,
    transpose: int = 0,
    dry_run: bool = False,
    progress_cb: Callable[[float], None] | None = None,
) -> Path | None:
    """PrettyPitchで歌唱WAVを生成し、共通の ``neutrino/vocal.wav`` を返す。"""
    resolved_root = configured_root(root)
    error = installation_error(resolved_root, python, leapsinger_root)
    if error is not None:
        raise RuntimeError(error)
    assert resolved_root is not None
    resolved_python = configured_python(resolved_root, python)
    resolved_leapsinger = configured_leapsinger_root(resolved_root, leapsinger_root)
    resolved_device = configured_device(device)
    resolved_speaker = configured_speaker_id(speaker_id)

    work_dir = Path(project_dir).resolve() / "prettypitch"
    output = Path(project_dir).resolve() / "neutrino" / "vocal.wav"
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    score = work_dir / "score.ust"
    mora_table = work_dir / "ja.mora"
    score.write_bytes(build_ust(project, lyric_map, transpose=transpose).encode("cp932"))
    _write_mora_table(resolved_root, mora_table)

    command = [
        str(resolved_python),
        "-m",
        "svs.render",
        str(score),
        "-o",
        str(output),
        "--spk_id",
        str(resolved_speaker),
        "--device",
        resolved_device,
        "--seed",
        "0",
        "--mora_table",
        str(mora_table),
        "--leapsinger_root",
        str(resolved_leapsinger),
    ]
    if dry_run:
        print(" ".join(command))
        return None
    if progress_cb is not None:
        progress_cb(0.0)
    proc = runproc.run(
        command,
        cwd=resolved_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "PrettyPitch歌声合成に失敗しました\n"
            f"stdout: {proc.stdout[-2000:]}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("PrettyPitchが歌唱WAVを生成しませんでした")
    if progress_cb is not None:
        progress_cb(1.0)
    runproc.log_generated_path(logger, "PrettyPitchで歌唱wavを合成しました", output)
    return output
