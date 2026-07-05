"""カナのモーラ分割ユーティリティ。

替え歌単語の読みを音符に割り当てるときの単位。
拗音(キャ等)は直前のカナにまとめ、長音「ー」も直前にまとめる。
"""

from __future__ import annotations

_SMALL = set("ャュョァィゥェォヮ")
_JOIN = _SMALL | {"ー"}


def split_moras(kana: str) -> list[str]:
    moras: list[str] = []
    for ch in kana:
        if moras and ch in _JOIN:
            moras[-1] += ch
        else:
            moras.append(ch)
    return moras
