"""NEUTRINO(歌声合成)のラッパー。

NEUTRINOは同梱しない。https://studio-neutrino.com/ から取得して展開し、
環境変数 NEUTRINO_ROOT でルートディレクトリを指定する。

実行コマンドはv3.2系(Tau)のRun.shに合わせたテンプレートを既定とし、
プロジェクトディレクトリの neutrino/commands.json で差し替えられる
(NEUTRINOのバージョンによりCLIが異なるため。v1系はWORLD/NSFの
後段が別コマンドだが、v3.2はneutrino単体でwavまで出力する)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from collections.abc import Callable
from pathlib import Path

from . import runproc

logger = logging.getLogger(__name__)

COMMANDS_FILENAME = "commands.json"

# NEUTRINO(v3.2 Tau)のneutrinoバイナリは合成中、標準出力に \r で上書きしながら
#   「    progress = 42 % (18.1 / 43.2 sec)」
# のような進捗行を出す。ここから割合を取り出す。
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)\s*%")


def parse_progress(line: str) -> float | None:
    """NEUTRINOの進捗行から進捗割合(0.0〜1.0)を取り出す。該当しなければ None。"""
    m = _PROGRESS_RE.search(line)
    if m is None:
        return None
    return max(0.0, min(1.0, int(m.group(1)) / 100.0))


MODEL_INFO_FILENAME = "model_info.json"

# 音名(科学的表記: 音名+任意の臨時記号+オクターブ)→ 半音オフセット。C4 = MIDI 60。
_NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_RE = re.compile(r"([A-G])([#♯b♭]?)(-?\d+)")
# settings/model_info.json の説明文にある
#   「推奨音域：mid2A～hiE（A3～E5）」
# の丸括弧内(科学的表記)を取り出す。区切りは全角/半角チルダやwave dashが混在する。
# Yamaha表記(mid2A等)ではなく丸括弧内のA3/E5側を使う(音名+オクターブで機械処理しやすい)。
_RANGE_RE = re.compile(
    r"推奨音域[^（(]*[（(]\s*"
    r"([A-G][#♯b♭]?-?\d+)\s*[～~〜]\s*([A-G][#♯b♭]?-?\d+)\s*[）)]"
)


def note_name_to_midi(name: str) -> int:
    """科学的表記の音名(例 A3, C#5, Bb2)を MIDI ノート番号に変換する。C4=60。"""
    m = _NOTE_RE.fullmatch(name.strip())
    if m is None:
        raise ValueError(f"音名として解釈できません: {name!r}")
    letter, accidental, octave = m.groups()
    semitone = _NOTE_OFFSETS[letter]
    if accidental in ("#", "♯"):
        semitone += 1
    elif accidental in ("b", "♭"):
        semitone -= 1
    return semitone + (int(octave) + 1) * 12


def parse_pitch_range(description: str) -> tuple[int, int] | None:
    """model_info.json のモデル説明文から推奨音域(MIDIのlo, hi)を取り出す。

    「推奨音域：…（A3～E5）」のような表記が無い/音名が解釈できない場合は None。
    """
    m = _RANGE_RE.search(description)
    if m is None:
        return None
    try:
        lo = note_name_to_midi(m.group(1))
        hi = note_name_to_midi(m.group(2))
    except ValueError:
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def model_pitch_range(
    model: str, root: Path | None = None
) -> tuple[int, int] | None:
    """NEUTRINOモデルの推奨音域(MIDIのlo, hi)を settings/model_info.json から読む。

    root 省略時は NEUTRINO_ROOT から探す。ファイルが無い・モデル名不一致・パース失敗・
    音域表記が無いときは None を返す(呼び出し側は汎用音域へフォールバックする)。
    実NEUTRINOを必要とせずテストできるよう、root と純粋なパース関数を分けてある。
    """
    try:
        if root is None:
            root = neutrino_root()
        raw = (root / "settings" / MODEL_INFO_FILENAME).read_text(encoding="utf-8")
        info = json.loads(raw)
    except (RuntimeError, OSError, ValueError):
        # RuntimeError: NEUTRINO_ROOT未設定/不正、OSError: ファイル無し、
        # ValueError: JSONパース失敗(json.JSONDecodeErrorを含む)
        return None
    if not isinstance(info, dict):
        return None
    # まず完全一致、無ければ大文字化して照合(JSONのキーは大文字)
    description = info.get(model) or info.get(model.upper())
    if not isinstance(description, str) or not description:
        return None
    return parse_pitch_range(description)

# NEUTRINO v3.2系(Tau) Run.sh 相当。プレースホルダは format() で展開される
DEFAULT_COMMANDS = [
    "{root}/bin/musicXMLtoLabel {musicxml} {full_lab} {mono_lab}",
    "{root}/bin/neutrino {full_lab} {timing_lab} {f0} {melspec} {wav} {root}/model/{model}/"
    " -n {threads} -k 0 -f 0 -s 48000 -b 16 -t",
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
    progress_cb: Callable[[float], None] | None = None,
) -> Path | None:
    """MusicXMLから歌唱wavを合成して work_dir/vocal.wav を返す。

    progress_cb を渡すと、NEUTRINOの進捗出力をパースして割合(0.0〜1.0)を
    その都度コールバックする。出力に進捗が無いバージョンでは呼ばれない。
    """
    root = None if dry_run else neutrino_root()
    work_dir.mkdir(parents=True, exist_ok=True)
    # 実行時はcwdをNEUTRINO_ROOTにするため、パスはすべて絶対にする
    work_dir = work_dir.resolve()
    musicxml_path = musicxml_path.resolve()
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
        "melspec": str(work_dir / "score.melspec"),
        "mgc": str(work_dir / "score.mgc"),
        "bap": str(work_dir / "score.bap"),
        "wav": str(wav),
    }
    commands = [c.format(**mapping) for c in load_commands(work_dir)]
    if dry_run:
        for c in commands:
            print(c)
        return None
    env = dict(os.environ)
    if root is not None:
        # v3.2のバイナリは同梱の共有ライブラリに依存する(Run.sh相当)。
        # macOSはDYLD_、LinuxはLD_を見るので両方設定しておく
        for var in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
            env[var] = f"{root / 'bin'}:{env.get(var, '')}"
    on_stdout = None
    if progress_cb is not None:
        def on_stdout(line: str) -> None:
            frac = parse_progress(line)
            if frac is not None:
                progress_cb(frac)

    for c in commands:
        logger.info("実行: %s", c)
        proc = runproc.run(
            shlex.split(c),
            cwd=root,  # NEUTRINOのバイナリは相対パス(settings等)に依存することがある
            env=env,
            capture_output=True,
            text=True,
            check=False,
            on_stdout=on_stdout,
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
