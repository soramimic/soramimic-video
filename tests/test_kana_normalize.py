from soramimic_video.kana import normalize_long_vowels


def test_normalize_ou_and_ei():
    assert normalize_long_vowels("トウキョウ") == "トーキョー"
    assert normalize_long_vowels("ケイサツ") == "ケーサツ"
    assert normalize_long_vowels("ヨウニ") == "ヨーニ"


def test_normalize_same_vowel_repetition():
    assert normalize_long_vowels("オオサカ") == "オーサカ"
    assert normalize_long_vowels("ニイガタ") == "ニーガタ"


def test_normalize_keeps_real_vowels():
    # ア段+ウ(ウタウ)は長音ではない
    assert normalize_long_vowels("ウタウ") == "ウタウ"
    # 既にーのものはそのまま
    assert normalize_long_vowels("ラーメン") == "ラーメン"


def test_normalize_idempotent():
    once = normalize_long_vowels("トウキョウ")
    assert normalize_long_vowels(once) == once
