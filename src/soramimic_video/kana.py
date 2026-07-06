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


def split_fine_moras(kana: str) -> list[str]:
    """拗音(小書きカナ)のみ直前に付ける細分割。ー・ッ・ンは独立要素。"""
    moras: list[str] = []
    for ch in kana:
        if moras and ch in _SMALL:
            moras[-1] += ch
        else:
            moras.append(ch)
    return moras


_VOWEL_ROWS = {
    "ア": "アカサタナハマヤラワガザダバパァャヮ",
    "イ": "イキシチニヒミリギジヂビピィ",
    "ウ": "ウクスツヌフムユルグズヅブプヴゥュ",
    "エ": "エケセテネヘメレゲゼデベペェ",
    "オ": "オコソトノホモヨロヲゴゾドボポォョ",
}
_CHAR_TO_VOWEL = {ch: v for v, chars in _VOWEL_ROWS.items() for ch in chars}


def vowel_of(kana: str) -> str | None:
    """カナ(モーラ)の母音を返す。ン・ッ・不明はNone。"""
    for ch in reversed(kana):
        if ch == "ー":
            continue
        return _CHAR_TO_VOWEL.get(ch)
    return None


def normalize_long_vowels(kana: str) -> str:
    """長音の表記ゆれを「ー」に正規化する(ヨウ→ヨー、ケイ→ケー、オオ→オー)。

    読みエンジンによって仮名形(トウキョウ)と発音形(トーキョー)が混在するため、
    比較・対応付けの前に揃える用途。
    """
    out: list[str] = []
    for ch in kana:
        prev_vowel = _CHAR_TO_VOWEL.get(out[-1]) if out else None
        if out and out[-1] != "ー":
            if (
                (ch == "ウ" and prev_vowel in ("オ", "ウ"))
                or (ch == "イ" and prev_vowel in ("エ", "イ"))
                or (ch in "アイウエオ" and prev_vowel == ch)
            ):
                out.append("ー")
                continue
        out.append(ch)
    return "".join(out)
