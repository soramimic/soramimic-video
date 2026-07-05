import pytest

pytest.importorskip("MeCab")
pytest.importorskip("ipadic")

from soramimic_video.reading import text_to_kana  # noqa: E402


def test_text_to_kana_kanji():
    assert text_to_kana("東京") == "トウキョウ"


def test_text_to_kana_mixed():
    assert text_to_kana("沈むように") == "シズムヨウニ"


def test_text_to_kana_keeps_kana_oov():
    # ひらがな・カタカナはそのまま読みになる
    assert "ラ" in text_to_kana("ラララ")


def test_text_to_kana_drops_symbols():
    result = text_to_kana("あ、い!")
    assert result == "アイ"
