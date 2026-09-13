from __future__ import annotations

import os
from pathlib import Path

import soramimic_video


def test_configure_numba_cache_uses_per_uid_xdg_directory(monkeypatch) -> None:
    monkeypatch.delenv("NUMBA_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/shared-cache")

    soramimic_video._configure_numba_cache()

    getuid = getattr(os, "getuid", None)
    identity = str(getuid()) if getuid is not None else os.environ.get("USERNAME", "user")
    assert Path(os.environ["NUMBA_CACHE_DIR"]) == (
        Path("/tmp/shared-cache") / "soramimic-video" / "numba" / identity
    )


def test_configure_numba_cache_preserves_operator_override(monkeypatch) -> None:
    monkeypatch.setenv("NUMBA_CACHE_DIR", "/srv/custom-numba-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/ignored")

    soramimic_video._configure_numba_cache()

    assert os.environ["NUMBA_CACHE_DIR"] == "/srv/custom-numba-cache"
