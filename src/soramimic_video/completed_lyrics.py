"""Portable editing result retained alongside a completed video."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project import Project

FILENAME = "lyrics.soramimic.json"
MAX_BYTES = 4 * 1024 * 1024
logger = logging.getLogger(__name__)


def result_path(directory: Path) -> Path | None:
    path = directory / FILENAME
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
            return None
        return path
    except OSError:
        return None


def save_result(
    project: Project, directory: Path, *, title: str = "", text_only_label: str = "",
) -> None:
    """Keep editable lyrics, never session URLs, uploads, or server-local paths.

    An oversized custom list must not prevent an otherwise valid video render.
    Such jobs have no editing link, just like older jobs without this artifact.
    """
    from .convert import (
        WORDLISTS_DIR,
        pop_note_length_weight,
        project_note_length_weights,
        resolve_wordlist,
    )
    from .editor_io import export_editor, seed_with_lyrics
    from .ruby import strip_ruby
    from .wordlist_csv import parse_editor_text

    path = directory / FILENAME
    try:
        if project.parody is None:
            return
        entry: dict[str, Any] | None = None
        wordlist = resolve_wordlist(project.parody.wordlist)
        if not wordlist.resolve().is_relative_to(WORDLISTS_DIR.resolve()):
            # Uploaded and editor-owned lists resolve to a local normalized CSV.
            # Preserve IDs while removing image paths that cannot cross apps.
            entry = {
                "value": "ORIGINAL", "text": "自作リスト",
                "csvText": parse_editor_text(wordlist.read_text(encoding="utf-8")).text,
            }
            if text_only_label:
                entry.update(text=text_only_label, textOnly=True)
        raw = json.loads((directory / "soramimic_raw.json").read_text(encoding="utf-8"))
        units = [line["units"] for line in raw["lines"]]
        weights = project_note_length_weights(project, 1.0)(units)
        alpha = pop_note_length_weight(dict(project.parody.params))
        export_editor(
            project, directory, wordlist_entry=entry, note_length_raw_list=weights,
            note_length_alpha=alpha, filename=FILENAME,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        seed_with_lyrics(payload, "\n".join(line.original_text or line.xf_kana
                                          for line in project.lines))
        payload["originalLines"] = [strip_ruby(line.original_text or line.xf_kana)
                                    for line in project.lines]
        payload["song"] = {"title": title}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            path.unlink(missing_ok=True)
            return
        path.write_bytes(encoded)
    except Exception:
        path.unlink(missing_ok=True)
        logger.exception("Could not prepare the optional editing result")
