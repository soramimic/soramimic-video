"""soramimic editor との連携(JSONファイルの書き出し/取り込み)。

soramimic の編集ツールは `soramimic-editor/1` 形式のJSON
(phrases/tokensList/results/param/wordlist/unitsList)を
読み込み/書き出しできる(soramimic#51)。

- export_editor: convert時に保存した変換の生応答から editor 用JSONを作る
- import_editor: editorで編集・書き出したJSONから project.parody を作り直す
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .convert import REPO_ROOT, apply_converted_lines, resolve_wordlist
from .project import Project

logger = logging.getLogger(__name__)

RAW_FILENAME = "soramimic_raw.json"
EDITOR_FILENAME = "editor.json"
EXPORT_FORMAT = "soramimic-editor/1"

SETTING_JSON = REPO_ROOT / "external" / "soramimic" / "conf" / "setting.json"


def save_raw(raw: dict, project_dir: Path) -> Path:
    path = project_dir / RAW_FILENAME
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _wordlist_entry(name: str, where: str | None) -> dict[str, Any]:
    """editorのconf(setting.json)と同じ形の単語リスト設定を返す。"""
    try:
        conf = json.loads(SETTING_JSON.read_text(encoding="utf-8"))
        for entry in conf.get("wordlist", []):
            if entry.get("filepath", "").endswith(f"/{name}.csv"):
                entry = dict(entry)
                if where is not None:
                    entry["where"] = where
                return entry
    except OSError:
        logger.warning("conf/setting.json が読めません(汎用の単語リスト設定を使います)")
    entry = {
        "value": name.upper(),
        "text": name,
        "filepath": f"wordlists/{name}.csv",
        "dbtype": "tidy",
    }
    if where is not None:
        entry["where"] = where
    return entry


def export_editor(project: Project, project_dir: Path) -> Path:
    """editorの「読み込み」で開けるJSONを書き出す。"""
    if project.parody is None:
        raise ValueError("替え歌案がありません。先に convert を実行してください")
    raw_path = project_dir / RAW_FILENAME
    if not raw_path.exists():
        raise ValueError(
            f"{raw_path} がありません。convert を実行し直してください"
            "(旧バージョンのconvert結果には編集ツール連携用のデータが含まれません)"
        )
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    # 現在のparodyのlocked状態をresultsに反映する(単語数が一致する行のみ)
    results = [line["words"] for line in raw["lines"]]
    for pline, words in zip(project.parody.lines, results, strict=True):
        if len(pline.words) == len(words):
            for w, raw_w in zip(pline.words, words, strict=True):
                raw_w["locked"] = w.locked

    payload = {
        "format": EXPORT_FORMAT,
        "phrases": raw.get("phrases", [ln.xf_kana for ln in project.lines]),
        "tokensList": raw.get("tokensList", []),
        "results": results,
        "param": project.parody.params,
        "wordlist": _wordlist_entry(
            resolve_wordlist(project.parody.wordlist).stem, project.parody.where
        ),
        "unitsList": [line["units"] for line in raw["lines"]],
    }
    path = project_dir / EDITOR_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def import_editor(project: Project, project_dir: Path, file: Path | None = None) -> None:
    """editorが書き出したJSONを取り込み、project.parodyを作り直す。

    convert を経ていないプロジェクトにも取り込める(JSON側のwordlist情報を使う)。
    ブラウザ(soramimic.com)で変換・編集した結果だけを持ち込むケース用で、
    このときローカル/Colab側では変換処理(soramimic ライブラリ)が不要になる。
    """
    path = file or (project_dir / EDITOR_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    units_list = payload.get("unitsList")
    if not isinstance(results, list) or not isinstance(units_list, list):
        raise ValueError("editorの書き出しファイルではありません(results/unitsListが必要)")
    if len(results) != len(project.lines):
        raise ValueError(
            f"行数が合いません: editor={len(results)}行, project={len(project.lines)}行"
        )

    lines = [
        {"units": units, "words": words}
        for units, words in zip(units_list, results, strict=True)
    ]
    # 単語リストはJSON側の情報(filepath)が解決できるならそちらを優先し、
    # だめなら既存のparody(convert済みの場合)にフォールバックする
    wordlist = project.parody.wordlist if project.parody else None
    where = project.parody.where if project.parody else None
    entry = payload.get("wordlist") or {}
    filepath = entry.get("filepath", "")
    if filepath.endswith(".csv"):
        # まずパスそのもの、だめならリスト名(stem)で解決を試みる
        for candidate in (filepath, Path(filepath).stem):
            try:
                resolve_wordlist(candidate)
                wordlist = candidate
                where = entry.get("where")
                break
            except FileNotFoundError:
                continue
        else:
            logger.warning(
                "editor JSONの単語リスト %s が見つからないため %s を使います", filepath, wordlist
            )
    if wordlist is None:
        raise ValueError(
            "単語リストを特定できません。convert を先に実行するか、"
            "wordlist情報(filepath)を含むeditor JSONを使ってください"
        )
    default_params = project.parody.params if project.parody else {}
    apply_converted_lines(
        project,
        lines,
        wordlist=wordlist,
        where=where,
        params=payload.get("param") or default_params,
    )
    # 再書き出しできるよう生応答も更新する
    save_raw(
        {
            "lines": lines,
            "tokensList": payload.get("tokensList", []),
            "phrases": payload.get("phrases", []),
        },
        project_dir,
    )
