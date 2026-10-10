"""Prepare and validate edits made between conversion and video generation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .convert import pop_note_length_weight, project_note_length_weights
from .editor_io import export_editor, import_editor, seed_with_lyrics
from .project import Project

REVIEW_FILENAME = "lyric-review.json"
MAX_REVIEW_BYTES = 5 * 1024 * 1024


class ReviewRequired(Exception):
    """Release the worker while the user reviews the converted lyrics."""


def prepare_review(
    project: Project, directory: Path, *, custom_csv: Path | None = None, title: str = ""
) -> None:
    entry = None
    editor_path = directory / "editor.json"
    if editor_path.exists():
        entry = json.loads(editor_path.read_text(encoding="utf-8")).get("wordlist")
    elif custom_csv is not None:
        entry = {
            "value": "ORIGINAL",
            "text": custom_csv.stem,
            "csvText": custom_csv.read_text(encoding="utf-8"),
        }
    raw = json.loads((directory / "soramimic_raw.json").read_text(encoding="utf-8"))
    units = [line["units"] for line in raw["lines"]]
    weights = project_note_length_weights(project, 1.0)(units)
    alpha = pop_note_length_weight(dict(project.parody.params if project.parody else {}))
    path = export_editor(
        project, directory, wordlist_entry=entry,
        note_length_raw_list=weights, note_length_alpha=alpha,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Use the analyzed text, including automatic transcription, for the editor.
    seed_with_lyrics(payload, "\n".join(line.original_text or line.xf_kana
                                       for line in project.lines))
    payload["song"] = {"title": title}
    payload["host"] = {"songs": [], "canUploadSong": False}
    (directory / REVIEW_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def validate_review(
    directory: Path, payload: Any, *, sessions_dir: Path | None = None
) -> bytes:
    """Validate before resuming, keeping source timing and wordlist paths trusted."""
    seed = json.loads((directory / REVIEW_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format", seed["format"]) != seed["format"]:
        raise ValueError("編集データの形式が正しくありません")
    if payload.get("phrases") != seed["phrases"]:
        raise ValueError("確認中は曲の読みを変更できません。曲を変える場合は生成し直してください")
    entry = payload.get("wordlist")
    if not isinstance(entry, dict) or any(
        entry.get(key) != seed["wordlist"].get(key)
        for key in ("value", "filepath", "csvText")
    ):
        raise ValueError("確認中は単語リストを変更できません。元のリストに戻してください")
    # Ignore client-provided paths, host controls and original-lyric replacements.
    accepted = dict(seed)
    for key in ("results", "unitsList", "tokensList", "param", "where"):
        if key in payload:
            accepted[key] = payload[key]
    results, units_list = payload.get("results"), payload.get("unitsList")
    if not isinstance(results, list) or not isinstance(units_list, list):
        raise ValueError("替え歌の変換を完了してから確認してください")
    if len(results) != len(seed["phrases"]) or len(units_list) != len(results):
        raise ValueError("編集データの行数が解析結果と一致しません")
    if not isinstance(accepted.get("param"), dict):
        raise ValueError("変換設定の形式が正しくありません")
    for words, units in zip(results, units_list, strict=True):
        if not isinstance(words, list) or not isinstance(units, list):
            raise ValueError("編集データの行が正しくありません")
        for word in words:
            if not isinstance(word, dict):
                raise ValueError("編集データの単語が正しくありません")
            period = word.get("period")
            if (
                not isinstance(period, list) or len(period) != 2
                or any(type(value) is not int for value in period)
                or not 0 <= period[0] < period[1] <= len(units)
            ):
                raise ValueError("編集データの単語位置が正しくありません")
    encoded = json.dumps(accepted, ensure_ascii=False).encode("utf-8")
    # Exercise the same note mapping as the worker without modifying the saved analysis.
    with tempfile.TemporaryDirectory(prefix="soramimic-review-") as temporary:
        path = Path(temporary) / "editor.json"
        path.write_bytes(encoded)
        try:
            import_editor(Project.load(directory), Path(temporary), path, sessions_dir)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ValueError(
                "編集データを音符に対応づけられません。替え歌を確認してください"
            ) from exc
    return encoded
