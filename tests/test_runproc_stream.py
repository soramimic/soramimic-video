"""runproc の標準出力ストリーミング(on_stdout)のテスト。

NEUTRINOは進捗を \\r で上書きしながら出すので、\\n だけでなく \\r も
行区切りとして途中経過を取り出せることを確認する。
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

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


def test_public_mode_suppresses_unclaimed_subprocess_output(monkeypatch, capfd):
    internal = "/srv/soramimic/jobs/private-id/input.mid"
    monkeypatch.setenv(runproc.PUBLIC_ENV, "1")

    proc = runproc.run(
        [
            sys.executable,
            "-c",
            f"import sys; print('reading {internal} ... 1'); "
            f"print('failed {internal}', file=sys.stderr)",
        ]
    )

    assert proc.returncode == 0
    captured = capfd.readouterr()
    assert internal not in captured.out
    assert internal not in captured.err


@pytest.mark.parametrize(
    "message",
    [
        "VOICEVOXで歌唱wavを合成しました",
        "サムネ画像を生成しました",
        "画像クレジットを書き出しました",
    ],
)
def test_public_mode_generated_log_omits_path(monkeypatch, caplog, message):
    internal = "/srv/soramimic/jobs/private-id/thumbnail.png"
    monkeypatch.setenv(runproc.PUBLIC_ENV, "true")

    with caplog.at_level(logging.INFO, logger="soramimic_video.test"):
        runproc.log_generated_path(
            logging.getLogger("soramimic_video.test"), message, internal
        )

    assert message in caplog.text
    assert internal not in caplog.text


def test_private_mode_generated_log_keeps_path(monkeypatch, caplog):
    internal = "/srv/soramimic/jobs/private-id/thumbnail.png"
    monkeypatch.delenv(runproc.PUBLIC_ENV, raising=False)

    with caplog.at_level(logging.INFO, logger="soramimic_video.test"):
        runproc.log_generated_path(
            logging.getLogger("soramimic_video.test"), "サムネ画像を生成しました", internal
        )

    assert internal in caplog.text


def test_native_output_suppression_is_public_only(monkeypatch, capfd):
    internal = b"reading /srv/soramimic/private/user.csv ... 1\n"
    monkeypatch.setenv(runproc.PUBLIC_ENV, "1")
    with runproc.suppress_native_output_in_public_mode():
        os.write(1, internal)
        os.write(2, internal)
    public = capfd.readouterr()
    assert "/srv/soramimic/private" not in public.out
    assert "/srv/soramimic/private" not in public.err

    monkeypatch.delenv(runproc.PUBLIC_ENV)
    with runproc.suppress_native_output_in_public_mode():
        os.write(1, internal)
        os.write(2, internal)
    private = capfd.readouterr()
    assert "/srv/soramimic/private" in private.out
    assert "/srv/soramimic/private" in private.err
