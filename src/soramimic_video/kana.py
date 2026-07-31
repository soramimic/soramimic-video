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


SMALL_TO_LARGE = {
    "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ", "ヮ": "ワ",
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お", "ゎ": "わ",
}


def _to_katakana(ch: str) -> str:
    """母音判定用にひらがな1文字をカタカナへ(それ以外はそのまま)。"""
    return chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch


def normalize_small_vowels(kana: str) -> str:
    """同母音の小書き母音(セェ・ハァ)を通常の母音に開く(セエ・ハア)。

    変換エンジンのトークナイズは促音「ウッ」は1ユニットに畳むのに、同母音の
    小書き(セェ)は「セ」「ェ」と割ってしまい、「ェ」だけの発音に一致する単語が
    無いためその行の変換結果が丸ごと空になる。エンジンに渡す前に開いておくと
    「セエ」1ユニットとして扱われ、発音は変えずに変換できる。
    ファ・ティ・ウィのような異母音の組み合わせは拗音なのでそのまま残す。
    1文字→1文字の置換なので文字列長・モーラ位置は変わらない。
    """
    out: list[str] = []
    prev_vowel: str | None = None
    for ch in kana:
        large = SMALL_TO_LARGE.get(ch)
        # 直前のカナが無い/母音不明(ン・ッ)の単独小書きも大文字に開く
        if large is not None and prev_vowel in (
            None, _CHAR_TO_VOWEL.get(_to_katakana(large))
        ):
            ch = large
        if ch != "ー":  # 長音は直前の母音を引き継ぐ
            prev_vowel = _CHAR_TO_VOWEL.get(_to_katakana(ch))
        out.append(ch)
    return "".join(out)


def open_long_vowel_runs(kana: str) -> str:
    """連続する長音の2個目以降を直前モーラの母音に開く(ドーー→ドーオ、ヨーーー→ヨーオオ)。

    変換エンジンのトークナイズは「ドー」を1ユニットに畳むが、続く2個目以降の
    「ー」は単独ユニットになり、「ー」だけの発音に一致する単語が無いため
    その行の変換結果が丸ごと空になる(同母音の小書きと同型の問題)。エンジンに
    渡す前に母音へ開いておくと通常のユニットとして扱われ、発音をほぼ変えずに
    変換できる。直前の母音が特定できない連続長音(ンーー等)はそのまま残す。
    1文字→1文字の置換なので文字列長・モーラ位置は変わらない。
    """
    out: list[str] = []
    prev = ""
    for ch in kana:
        if ch == "ー" and prev == "ー":
            vowel = vowel_of("".join(out))
            if vowel is not None:
                ch = vowel
        else:
            prev = ch  # 連続長音の途中(母音に開いた位置)でも元の並びで連続を判定する
        out.append(ch)
    return "".join(out)


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
