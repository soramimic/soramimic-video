"""歌詞テキストのカナ読み変換(MeCab + unidic-lite)。

辞書は unidic-lite を使う(ipadic は「二人」→「ニニン」等の誤読が多く、
読みの誤りはアライメント先の取り違えとして下流全体に伝播するため)。
読みには仮名形・出現形(フィールド17)を使う: 活用形に追従し、
かつ「トウキョウ」式(ーでなく母音字)なのでモーラ数が歌唱の音符数と対応する。
"""

from __future__ import annotations

import csv
import logging
import re
from typing import Any

import jaconv

logger = logging.getLogger(__name__)

_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")
_KANA_FIELD = 17  # unidic: 仮名形(出現形)
_PRON_FIELD = 9  # unidic: 発音形(出現形)。仮名形が無いときのフォールバック

_tagger: Any = None


def _get_tagger() -> Any:
    global _tagger
    if _tagger is None:
        try:
            import MeCab
            import unidic_lite
        except ImportError as e:
            raise RuntimeError(
                "mecab-python3 / unidic-lite がインストールされていません"
                "(uv sync --extra audio)"
            ) from e
        _tagger = MeCab.Tagger("-d " + unidic_lite.DICDIR)
    return _tagger


def _feature_fields(feature: str) -> list[str]:
    """unidicのfeature文字列をパースする(引用符内のカンマを含むフィールドがある)。"""
    return next(csv.reader([feature]))


def text_to_kana(text: str) -> str:
    """漢字かな交じりの歌詞1行をカタカナ読みにする。

    読みが取れない部分(記号・英字など)は警告して無視する。
    """
    node = _get_tagger().parseToNode(text)
    parts: list[str] = []
    while node:
        if node.surface:
            fields = _feature_fields(node.feature)
            reading = next(
                (
                    fields[i]
                    for i in (_KANA_FIELD, _PRON_FIELD)
                    if len(fields) > i and fields[i] not in ("", "*")
                ),
                None,
            )
            if reading is None:
                # 未知語: 既にカナならそのまま読みにする
                reading = jaconv.hira2kata(node.surface)
                if not _KATAKANA_RE.fullmatch(reading):
                    logger.warning("読みが取れないため無視: %r", node.surface)
            parts.append(reading)
        node = node.next
    return "".join(_KATAKANA_RE.findall("".join(parts)))
