import pytest

from soramimic_video.word_reading import restore_word_reading


@pytest.mark.parametrize(("kana", "allocated", "expected"), [
    ("リン", ["リー"], ["リン"]),
    ("バンビ", ["バー", "ビ"], ["バン", "ビ"]),
    ("テンジン", ["テ", "ジン"], ["テン", "ジン"]),
    ("カッパ", ["カ", "パ"], ["カッ", "パ"]),
    ("リン", ["リ", ""], ["リ", "ン"]),
    ("リン", ["リ", "ー"], ["リ", "ン"]),
    ("リン", ["リー", "ー"], ["リン", "ー"]),
    ("キャット", ["キャー", "ト"], ["キャッ", "ト"]),
    ("トキワシイラ", ["ト", "キ", "ワ", "シ", "ラ"], ["ト", "キ", "ワ", "シイ", "ラ"]),
    ("セイショウナゴン", ["セ", "イ", "ショー", "ナ", "ゴ", "ン"],
     ["セ", "イ", "ショウ", "ナ", "ゴ", "ン"]),
    ("ハビー", ["ハ", "ビー", ""], ["ハ", "ビー", ""]),
    ("モ", ["モ", "", "ー"], ["モ", "", "ー"]),
    ("アア", ["ア", "ア"], ["ア", "ア"]),
    ("カキ", ["", ""], ["カ", "キ"]),
    ("ンカ", ["カ"], ["ンカ"]),
])
def test_restore_word_reading(kana, allocated, expected):
    before = allocated.copy()
    assert restore_word_reading(kana, allocated) == expected
    assert allocated == before


def test_restoration_requires_a_note():
    with pytest.raises(ValueError, match="音符がありません"):
        restore_word_reading("リン", [])
