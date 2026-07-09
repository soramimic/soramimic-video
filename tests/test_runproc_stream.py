"""runproc の標準出力ストリーミング(on_stdout)のテスト。

NEUTRINOは進捗を \\r で上書きしながら出すので、\\n だけでなく \\r も
行区切りとして途中経過を取り出せることを確認する。
"""

from __future__ import annotations

import sys

from soramimic_video import runproc


def test_run_streams_stdout_split_by_cr_and_lf():
    # \r で上書き表示する進捗と、最後に \n 終端の行を出すスクリプト
    script = (
        "import sys\n"
        "for i in (0, 50, 100):\n"
        "    sys.stdout.write(f'    progress = {i} % (x / y sec)\\r')\n"
        "    sys.stdout.flush()\n"
        "sys.stdout.write('done\\n')\n"
    )
    lines: list[str] = []
    proc = runproc.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        on_stdout=lines.append,
    )
    assert proc.returncode == 0
    # \r 区切りの各進捗行がコールバックされている
    assert any("progress = 0 %" in ln for ln in lines)
    assert any("progress = 50 %" in ln for ln in lines)
    assert "done" in lines
    # エラー報告用に全文も保持されている
    assert "progress = 100 %" in proc.stdout


def test_run_streaming_callback_error_does_not_break_run():
    def boom(_line: str) -> None:
        raise ValueError("callback failed")

    proc = runproc.run(
        [sys.executable, "-c", "print('hello')"],
        capture_output=True,
        text=True,
        on_stdout=boom,
    )
    assert proc.returncode == 0
    assert "hello" in proc.stdout
