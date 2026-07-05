import pytest

pytest.importorskip("MeCab")
pytest.importorskip("unidic_lite")

from soramimic_video.reading import text_to_kana  # noqa: E402


def test_text_to_kana_kanji():
    assert text_to_kana("東京") == "トウキョウ"


def test_text_to_kana_mixed():
    assert text_to_kana("沈むように") == "シズムヨウニ"


def test_text_to_kana_futari():
    # ipadicは「二人」を「ニニン」と誤読していた(unidic-liteに切り替えた理由)
    assert text_to_kana("二人だけの空が広がる夜に") == "フタリダケノソラガヒロガルヨルニ"


def test_text_to_kana_conjugation():
    # 仮名形は活用形(出現形)に追従する(語彙素読みだとヒロガルになってしまう)
    assert text_to_kana("広がって") == "ヒロガッテ"


def test_text_to_kana_keeps_kana_oov():
    # ひらがな・カタカナはそのまま読みになる
    assert "ラ" in text_to_kana("ラララ")


def test_text_to_kana_drops_symbols():
    result = text_to_kana("あ、い!")
    assert result == "アイ"
