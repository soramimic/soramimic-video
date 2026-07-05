"""歌詞テキストのカナ読み変換(MeCab + ipadic)。"""

from __future__ import annotations

import logging
import re
from typing import Any

import jaconv

logger = logging.getLogger(__name__)

_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")

_tagger: Any = None


def _get_tagger() -> Any:
    global _tagger
    if _tagger is None:
        try:
            import ipadic
            import MeCab
        except ImportError as e:
            raise RuntimeError(
                "mecab-python3 / ipadic がインストールされていません"
                "(uv sync --extra audio)"
            ) from e
        _tagger = MeCab.Tagger(ipadic.MECAB_ARGS)
    return _tagger


def text_to_kana(text: str) -> str:
    """漢字かな交じりの歌詞1行をカタカナ読みにする。

    読みが取れない部分(記号・英字など)は警告して無視する。
    """
    node = _get_tagger().parseToNode(text)
    parts: list[str] = []
    while node:
        if node.surface:
            feats = node.feature.split(",")
            reading = feats[7] if len(feats) > 7 and feats[7] != "*" else None
            if reading is None:
                # 未知語: 既にカナならそのまま読みにする
                reading = jaconv.hira2kata(node.surface)
                if not _KATAKANA_RE.fullmatch(reading):
                    logger.warning("読みが取れないため無視: %r", node.surface)
            parts.append(reading)
        node = node.next
    return "".join(_KATAKANA_RE.findall("".join(parts)))
