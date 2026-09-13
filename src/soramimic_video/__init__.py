"""soramimic-video: XF MIDIと歌詞から替え歌動画を作るパイプライン。"""

from __future__ import annotations

import os
from pathlib import Path


def _configure_numba_cache() -> None:
    """Keep Numba's generated cache writable for the current OS account.

    Deploy-time warmups run as root while the API runs as an unprivileged account.
    Both intentionally share ``HOME``.  Numba's default cache would consequently
    let the warmup create a release-specific directory that the API cannot write.
    An explicit, per-uid cache root keeps those writers isolated.
    """
    if os.environ.get("NUMBA_CACHE_DIR"):
        return

    cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_root = Path(cache_home) if cache_home else Path.home() / ".cache"
    getuid = getattr(os, "getuid", None)
    identity = str(getuid()) if getuid is not None else os.environ.get("USERNAME", "user")
    os.environ["NUMBA_CACHE_DIR"] = str(cache_root / "soramimic-video" / "numba" / identity)


_configure_numba_cache()

__version__ = "0.1.0"
