from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from soramimic_video import custom_thumbnail_preview as custom
from soramimic_video import thumbnail
from soramimic_video.thumbnail_preview import render_slot


def test_worker_disables_db_cache_and_uses_list_label(tmp_path, monkeypatch):
    (tmp_path / "wordlist.csv").write_text(
        "id,surface,original,pronunciation\n1,ねこ,ねこ,ネコ", encoding="utf-8",
    )
    (tmp_path / "request.json").write_text(json.dumps({
        "title": "猫", "title_kana": "ネコ", "wordlist_name": "自分の動物",
    }), encoding="utf-8")
    seen = []

    def convert(phrases, path, where, params, *, cache_db):
        assert cache_db is False
        assert phrases == ["ネコ"]
        assert path == tmp_path / "wordlist.csv"
        return {"lines": [{"words": [{"id": "1", "surface": "ねこ"}]}]}

    def draw(path, title, label, **kwargs):
        seen.append((path, title, label, kwargs))

    monkeypatch.setattr(thumbnail, "run_convert", convert)
    monkeypatch.setattr(thumbnail, "render_thumbnail", draw)
    custom.render_worker(tmp_path)
    path, title, label, kwargs = seen[0]
    assert path == tmp_path / "preview.png"
    assert title == "猫"
    assert label == "自分の動物"
    assert kwargs["words"] == ["ねこ"]
    assert "image_paths" not in kwargs


@pytest.mark.parametrize("outcome", ["success", "timeout", "error"])
def test_private_files_removed_on_every_worker_exit(monkeypatch, outcome):
    directories = []

    def run(cmd, **kwargs):
        directory = Path(cmd[-1])
        directories.append(directory)
        assert (directory / "wordlist.csv").read_text() == "private csv"
        assert kwargs["timeout"] > 0
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        (directory / "preview.png").write_bytes(b"private png")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if outcome == "error":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(custom.subprocess, "run", run)
    if outcome == "success":
        assert custom.render_custom_preview("private csv", "title", "../list") == b"private png"
    else:
        with pytest.raises(TimeoutError if outcome == "timeout" else ValueError):
            custom.render_custom_preview("private csv", "title", "../list")
    assert len(directories) == 1
    assert not directories[0].exists()


def test_custom_preview_does_not_queue_more_workers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("busy previews must not start a worker")

    monkeypatch.setattr(custom.subprocess, "run", forbidden)
    with render_slot(), pytest.raises(TimeoutError):
        custom.render_custom_preview("private csv", "title", "list")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker limits")
def test_worker_limits_preserve_stricter_inherited_limits(monkeypatch):
    import resource

    limits = {
        resource.RLIMIT_AS: (512 * 1024**2, resource.RLIM_INFINITY),
        resource.RLIMIT_CPU: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        resource.RLIMIT_CORE: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    }
    applied = {}
    monkeypatch.setattr(resource, "getrlimit", limits.__getitem__)
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: applied.update({kind: value}))
    custom._limit_worker()
    assert applied[resource.RLIMIT_AS] == (512 * 1024**2,) * 2
    assert applied[resource.RLIMIT_CPU] == (custom.WORKER_TIMEOUT_SECONDS,) * 2
    assert applied[resource.RLIMIT_CORE] == (0, 0)
