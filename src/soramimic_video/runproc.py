"""サブプロセス実行の薄いラッパー(ジョブ中断用)。

パイプラインの重い処理(NEUTRINO・ffmpeg・fluidsynth など)はすべて
ここを通して起動する。実行中のプロセスを登録しておき、APIの中断
リクエストから kill_current() でプロセスグループごと止められる。
ワーカーは1本なので「現在のプロセス」は同時に1つしかない。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable

_lock = threading.Lock()
_current: subprocess.Popen | None = None
_cancel_check: Callable[[], bool] | None = None


class Cancelled(Exception):  # noqa: N818 - 制御フロー用
    """中断リクエストにより処理を止めた。"""


def set_cancel_check(fn: Callable[[], bool] | None) -> None:
    """中断判定関数を登録する(ジョブ実行の前後でワーカーが設定・解除する)。"""
    global _cancel_check
    _cancel_check = fn


def raise_if_cancelled() -> None:
    """中断が要求されていたら Cancelled を投げる。長いループの中などで呼ぶ。"""
    if _cancel_check is not None and _cancel_check():
        raise Cancelled()


def run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output対応)相当。実行中は kill_current の対象になる。"""
    global _current
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    check = kwargs.pop("check", False)
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    raise_if_cancelled()  # kill後の後続コマンドを起動しない
    # 新しいセッションにしておくと、プロセスグループごとkillでき、
    # 子(NEUTRINOが起動するプロセス等)も巻き添えにできる
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    with _lock:
        _current = proc
    try:
        stdout, stderr = proc.communicate(input=input_data)
    finally:
        with _lock:
            _current = None
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return result


def kill_current() -> bool:
    """実行中のプロセス(グループ)を止める。止めたらTrue。"""
    with _lock:
        proc = _current
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    return True
