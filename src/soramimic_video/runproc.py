"""サブプロセス実行の薄いラッパー(ジョブ中断用)。

パイプラインの重い処理(NEUTRINO・ffmpeg・fluidsynth など)はすべて
ここを通して起動する。実行中のプロセスを登録しておき、APIの中断
リクエストから kill_current() でプロセスグループごと止められる。
ワーカーは1本なので「現在のプロセス」は同時に1つしかない。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

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


def run(
    cmd, on_stdout: Callable[[str], None] | None = None, **kwargs
) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output対応)相当。実行中は kill_current の対象になる。

    on_stdout を渡すと、標準出力を行(改行・復帰=\\r 区切り)ごとに読み取って
    その都度コールバックする(NEUTRINOの進捗表示のような、途中経過を取り出す用途)。
    このときの読み取りはバイト単位で行い、こちらでUTF-8にデコードする。
    """
    global _current
    if kwargs.pop("capture_output", False):
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    check = kwargs.pop("check", False)
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if on_stdout is not None:
        # ストリーミング読み取りは生バイトで行うので、テキスト系の指定は外す
        for key in ("text", "universal_newlines", "encoding", "errors"):
            kwargs.pop(key, None)
    raise_if_cancelled()  # kill後の後続コマンドを起動しない
    # 新しいセッションにしておくと、プロセスグループごとkillでき、
    # 子(NEUTRINOが起動するプロセス等)も巻き添えにできる
    proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    with _lock:
        _current = proc
    try:
        if on_stdout is not None:
            stdout, stderr = _communicate_streaming(proc, on_stdout, input_data)
        else:
            stdout, stderr = proc.communicate(input=input_data)
    finally:
        with _lock:
            _current = None
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return result


def _emit(on_stdout: Callable[[str], None], line: str) -> None:
    try:
        on_stdout(line)
    except Exception:  # noqa: BLE001 - コールバックの失敗で本処理を止めない
        logger.exception("on_stdout コールバックでエラー")


def _communicate_streaming(
    proc: subprocess.Popen, on_stdout: Callable[[str], None], input_data
) -> tuple[str, str]:
    """標準出力を \\n / \\r 区切りの行として逐次コールバックしつつ全量も集める。

    NEUTRINOは進捗を \\r で上書き表示するため、\\n だけでなく \\r も行区切りとして扱う。
    エラー報告用に stdout/stderr の全文も返す(communicate 相当)。
    """
    if input_data is not None and proc.stdin is not None:
        proc.stdin.write(input_data)
        proc.stdin.close()
    out_buf = bytearray()
    err_buf = bytearray()

    def pump_stdout() -> None:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        seg = bytearray()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out_buf.extend(chunk)
            for byte in chunk:
                if byte in (0x0A, 0x0D):  # \n / \r
                    if seg:
                        _emit(on_stdout, seg.decode("utf-8", "replace"))
                        seg = bytearray()
                else:
                    seg.append(byte)
        if seg:
            _emit(on_stdout, seg.decode("utf-8", "replace"))
        proc.stdout.close()

    def pump_stderr() -> None:
        if proc.stderr is None:
            return
        fd = proc.stderr.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            err_buf.extend(chunk)
        proc.stderr.close()

    threads = []
    if proc.stdout is not None:
        threads.append(threading.Thread(target=pump_stdout))
    if proc.stderr is not None:
        threads.append(threading.Thread(target=pump_stderr))
    for t in threads:
        t.start()
    proc.wait()
    for t in threads:
        t.join()
    return out_buf.decode("utf-8", "replace"), err_buf.decode("utf-8", "replace")


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
