"""Render an uploaded text list in a disposable, bounded worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER_TIMEOUT_SECONDS = 20
WORKER_MEMORY_BYTES = 2 * 1024**3


def render_custom_preview(
    csv_text: str, title: str, wordlist_name: str, title_kana: str = "",
) -> bytes:
    from .thumbnail_preview import render_slot

    # A short wait lets an aborted UI request finish, without building a long queue.
    with render_slot(timeout=2), tempfile.TemporaryDirectory(
        prefix="soramimic-custom-preview-"
    ) as temporary:
        directory = Path(temporary)
        (directory / "wordlist.csv").write_text(csv_text, encoding="utf-8")
        (directory / "request.json").write_text(
            json.dumps(
                {"title": title, "title_kana": title_kana, "wordlist_name": wordlist_name},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [sys.executable, "-m", "soramimic_video.custom_thumbnail_preview", temporary],
                check=True,
                timeout=WORKER_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Custom preview exceeded its time limit") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError("Custom preview could not be rendered") from exc
        return (directory / "preview.png").read_bytes()


def render_worker(directory: Path) -> None:
    # Imports stay inside the worker so its memory limit covers engine loading.
    from .thumbnail import render_thumbnail, title_paraphrase
    from .thumbnail_preview import PREVIEW_HEIGHT, PREVIEW_WIDTH

    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    found = title_paraphrase(
        request["title_kana"] or request["title"],
        str(directory / "wordlist.csv"),
        None,
        None,
        cache_db=False,
    )
    render_thumbnail(
        directory / "preview.png",
        request["title"],
        request["wordlist_name"],
        words=[str(word.get("surface") or "") for word, _row in found],
        width=PREVIEW_WIDTH,
        height=PREVIEW_HEIGHT,
    )


def _limit_worker() -> None:
    if sys.platform == "linux":
        import resource

        for kind, maximum in (
            (resource.RLIMIT_AS, WORKER_MEMORY_BYTES),
            (resource.RLIMIT_CPU, WORKER_TIMEOUT_SECONDS),
            (resource.RLIMIT_CORE, 0),
        ):
            inherited = resource.getrlimit(kind)
            limit = min([maximum, *(v for v in inherited if v != resource.RLIM_INFINITY)])
            resource.setrlimit(kind, (limit, limit))


if __name__ == "__main__":
    _limit_worker()
    render_worker(Path(sys.argv[1]))
