"""Video側で持つ単語リストの表示・公開メタデータ。

単語リスト本体と絞り込み設定は submodule が正本だが、Video固有の表示名、
既定レイアウト、Simple UIでの公開可否はこのカタログを唯一の正本にする。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORDLIST_CATALOG_PATH = Path(__file__).resolve().parent / "wordlist_catalog.json"


def load_wordlist_catalog(path: Path = WORDLIST_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    """単語リストカタログを読む。壊れている場合は空のカタログとして扱う。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): entry
        for name, entry in raw.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


def default_launch_wordlists(path: Path = WORDLIST_CATALOG_PATH) -> list[str]:
    """Simple UIで公開する単語リストをカタログの記載順で返す。"""
    return [
        name
        for name, entry in load_wordlist_catalog(path).items()
        if entry.get("launch") is True
    ]
