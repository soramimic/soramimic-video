from soramimic_video.evaluate import match_by_kana


def test_match_by_kana_exact():
    pairs = match_by_kana(["ア", "イ", "ウ"], ["ア", "イ", "ウ"])
    assert pairs == [(0, 0), (1, 1), (2, 2)]


def test_match_by_kana_long_vowel_notation():
    # 正解がヨ+ウの2音符、推定がヨー式(ヨー+継続ー)でも両方対応がつく
    pairs = match_by_kana(["ヨ", "ウ", "ニ"], ["ヨー", "ー", "ニ"])
    matched_truth = {t for t, _ in pairs}
    assert matched_truth == {0, 1, 2}  # ウ音符も(ヨー系のどれかに)対応がつく


def test_match_by_kana_merged_note():
    # 推定が1音符にまとまっている場合は推定側の再利用で正解2音符とも対応
    pairs = match_by_kana(["ト", "ウ", "キョ", "ウ"], ["トー", "キョー"])
    matched_truth = {t for t, _ in pairs}
    assert matched_truth == {0, 1, 2, 3}


def test_match_by_kana_skips_mismatch():
    pairs = match_by_kana(["ア", "カ", "イ"], ["ア", "サ", "イ"])
    truth_matched = {t for t, _ in pairs}
    assert 0 in truth_matched and 2 in truth_matched
    assert 1 not in truth_matched
