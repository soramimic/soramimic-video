"""NEUTRINO(歌声合成)のラッパー。

NEUTRINOは同梱しない。https://studio-neutrino.com/ から取得して展開し、
環境変数 NEUTRINO_ROOT でルートディレクトリを指定する。

実行コマンドはv1系のRun.shに合わせたテンプレートを既定とし、
プロジェクトディレクトリの neutrino/commands.json で差し替えられる
(NEUTRINOのバージョンによりCLIが異なるため)。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

COMMANDS_FILENAME = "commands.json"

# NEUTRINO v1系 Run.sh 相当。プレースホルダは format() で展開される
DEFAULT_COMMANDS = [
    "{root}/bin/musicXMLtoLabel {musicxml} {full_lab} {mono_lab}",
    "{root}/bin/NEUTRINO {full_lab} {timing_lab} {f0} {mgc} {bap} {root}/model/{model}/"
    " -n {threads} -t",
    "{root}/bin/WORLD {f0} {mgc} {bap} -f 0.0 -m 1.0 -o {wav} -n {threads} -t",
]


def neutrino_root() -> Path:
    root = os.environ.get("NEUTRINO_ROOT")
    if not root:
        raise RuntimeError(
            "環境変数 NEUTRINO_ROOT が設定されていません。"
            "NEUTRINOを https://studio-neutrino.com/ から取得し、展開先を指定してください"
        )
    path = Path(root).expanduser()
    if not (path / "bin").exists():
        raise RuntimeError(f"NEUTRINO_ROOT が不正です(bin/がありません): {path}")
    return path


def load_commands(work_dir: Path) -> list[str]:
    """コマンドテンプレートを読む。無ければ既定値を書き出して使う。"""
    path = work_dir / COMMANDS_FILENAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    work_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(DEFAULT_COMMANDS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info("コマンドテンプレートを書き出しました(必要に応じて編集): %s", path)
    return list(DEFAULT_COMMANDS)


def run_neutrino(
    musicxml_path: Path,
    work_dir: Path,
    model: str = "MERROW",
    threads: int = 4,
    dry_run: bool = False,
) -> Path | None:
    """MusicXMLから歌唱wavを合成して work_dir/vocal.wav を返す。"""
    root = None if dry_run else neutrino_root()
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "vocal.wav"
    mapping = {
        "root": str(root) if root else "$NEUTRINO_ROOT",
        "model": model,
        "threads": str(threads),
        "musicxml": str(musicxml_path),
        "full_lab": str(work_dir / "full.lab"),
        "mono_lab": str(work_dir / "mono.lab"),
        "timing_lab": str(work_dir / "timing.lab"),
        "f0": str(work_dir / "score.f0"),
        "mgc": str(work_dir / "score.mgc"),
        "bap": str(work_dir / "score.bap"),
        "wav": str(wav),
    }
    commands = [c.format(**mapping) for c in load_commands(work_dir)]
    if dry_run:
        for c in commands:
            print(c)
        return None
    for c in commands:
        logger.info("実行: %s", c)
        proc = subprocess.run(
            shlex.split(c),
            cwd=root,  # NEUTRINOのバイナリは相対パス(settings等)に依存することがある
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"NEUTRINOコマンドが失敗しました: {c}\n"
                f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}\n"
                f"CLIが合わない場合は {work_dir / COMMANDS_FILENAME} を"
                "お使いのバージョンのRun.shに合わせて編集してください"
            )
    if not wav.exists():
        raise RuntimeError(f"歌唱wavが生成されませんでした: {wav}")
    return wav
