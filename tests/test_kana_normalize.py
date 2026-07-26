from soramimic_video.kana import normalize_long_vowels, normalize_small_vowels


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


def test_normalize_small_vowels_same_vowel_opens():
    # 同母音の小書きは通常の母音に開く(エンジンが「ェ」単独ユニットに割らないように)
    assert normalize_small_vowels("ウッセェワ") == "ウッセエワ"
    assert normalize_small_vowels("ハァ") == "ハア"
    assert normalize_small_vowels("リィ") == "リイ"
    assert normalize_small_vowels("キャァ") == "キャア"  # 拗音の母音(ア段)に続く小書き


def test_normalize_small_vowels_keeps_different_vowel():
    # 異母音の組み合わせ(拗音)はそのまま
    assert normalize_small_vowels("ファン") == "ファン"
    assert normalize_small_vowels("ティー") == "ティー"
    assert normalize_small_vowels("ウィキ") == "ウィキ"
    assert normalize_small_vowels("クヮガタ") == "クヮガタ"


def test_normalize_small_vowels_hiragana():
    assert normalize_small_vowels("うっせぇわ") == "うっせえわ"
    assert normalize_small_vowels("ふぁん") == "ふぁん"


def test_normalize_small_vowels_bare_small():
    # 直前が無い/母音不明(ン・ッ)の単独小書きは対応する大文字へ
    assert normalize_small_vowels("ェ") == "エ"
    assert normalize_small_vowels("ンァ") == "ンア"
    assert normalize_small_vowels("ッォ") == "ッオ"


def test_normalize_small_vowels_after_long_vowel():
    # 「ー」は直前の母音を引き継ぐので、セーェ は同母音として開く
    assert normalize_small_vowels("セーェ") == "セーエ"
    assert normalize_small_vowels("セーォ") == "セーォ"


def test_normalize_small_vowels_length_preserved():
    for s in ["ウッセェワ", "ファンタジィ", "ハァハァ", "トウキョウ"]:
        assert len(normalize_small_vowels(s)) == len(s)
