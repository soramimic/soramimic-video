"""人手編集ステージ: 替え歌案の書き出しと取り込み。

v1はファイルベース。export-edit が edit.json を書き出し、
人が surface / kana / locked を編集して import-edit で取り込む。
(soramimic editorとの直接連携は今後: DESIGN.md参照)
"""

from __future__ import annotations

import json
from pathlib import Path

from .kana import split_moras
from .project import Project

EDIT_FILENAME = "edit.json"


def export_edit(project: Project, project_dir: Path) -> Path:
    if project.parody is None:
        raise ValueError("替え歌案がありません。先に convert を実行してください")
    lines = []
    for pline in project.parody.lines:
        line = project.lines[pline.line_id]
        lines.append(
            {
                "line_id": pline.line_id,
                "xf_surface": line.xf_surface,
                "xf_kana": line.xf_kana,
                "original_text": line.original_text,
                "words": [
                    {
                        "surface": w.surface,
                        "kana": w.kana,
                        "original": w.original,
                        "originalkana": w.originalkana,
                        "note_ids": w.note_ids,  # 参照用(編集不可)
                        "locked": w.locked,
                    }
                    for w in pline.words
                ],
            }
        )
    path = project_dir / EDIT_FILENAME
    path.write_text(
        json.dumps({"lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def import_edit(project: Project, project_dir: Path) -> None:
    """edit.json の surface / kana / locked を project.parody に反映する。

    読みのモーラ数がその単語の音符数を超える場合はエラー
    (音符に載せられないため)。少ない分は合成時に母音を伸ばして埋める。
    """
    if project.parody is None:
        raise ValueError("替え歌案がありません。先に convert を実行してください")
    data = json.loads((project_dir / EDIT_FILENAME).read_text(encoding="utf-8"))
    by_id = {pl.line_id: pl for pl in project.parody.lines}
    errors: list[str] = []
    for eline in data["lines"]:
        pline = by_id.get(eline["line_id"])
        if pline is None:
            errors.append(f"line_id {eline['line_id']} はプロジェクトに存在しません")
            continue
        if len(eline["words"]) != len(pline.words):
            errors.append(
                f"行{eline['line_id']}: 単語数が変わっています "
                f"({len(pline.words)} -> {len(eline['words'])})。"
                "単語の追加・削除には対応していません(kanaの変更で調整してください)"
            )
            continue
        for w, ew in zip(pline.words, eline["words"], strict=True):
            kana = ew["kana"]
            n_moras = len(split_moras(kana))
            if n_moras > len(w.note_ids):
                errors.append(
                    f"行{eline['line_id']}: {ew['surface']!r} の読み {kana!r} は "
                    f"{n_moras}モーラで音符数 {len(w.note_ids)} を超えています"
                )
                continue
            w.surface = ew["surface"]
            w.kana = kana
            w.locked = bool(ew.get("locked", False))
            if ew.get("original"):
                w.original = ew["original"]
    if errors:
        raise ValueError("編集内容にエラーがあります:\n" + "\n".join(errors))
