from soramimic_video.align import align_texts


def test_align_one_to_one():
    xf = ["沈むように", "溶けてゆくように"]
    lyrics = ["沈むように", "溶けてゆくように"]
    assert align_texts(xf, lyrics) == [0, 1]


def test_align_two_xf_lines_to_one_lyric_line():
    xf = ["沈むように", "溶けてゆくように", "二人だけの空が", "広がる夜に"]
    lyrics = ["沈むように 溶けてゆくように", "二人だけの空が 広がる夜に"]
    assert align_texts(xf, lyrics) == [0, 0, 1, 1]


def test_align_skips_unsung_lyric_line():
    xf = ["沈むように", "二人だけの空が"]
    lyrics = ["沈むように", "(歌われないセリフの行)", "二人だけの空が"]
    assert align_texts(xf, lyrics) == [0, 2]


def test_align_xf_line_missing_from_lyrics():
    xf = ["ラララ", "沈むように"]
    lyrics = ["沈むように"]
    assert align_texts(xf, lyrics) == [None, 0]


def test_align_kana_line_matches_via_okurigana():
    # XF側が読みだけでも、送り仮名の重なりで元歌詞行に対応づく
    assert align_texts(["シズムヨウニ"], ["沈むように"]) == [0]


def test_align_kana_line_matches_kanji_heavy_lyric_via_reading():
    import pytest

    pytest.importorskip("MeCab")
    # 表記の重なりがゼロ(漢字だらけの行)でも、元歌詞側を読みに変換して対応づく
    xf = ["ゲッコウ", "シズムヨウニ"]
    lyrics = ["月光", "沈むように"]
    assert align_texts(xf, lyrics) == [0, 1]


def test_align_reading_absorbs_long_vowel_variants():
    import pytest

    pytest.importorskip("MeCab")
    # XFカナが仮名形(トウキョウ)でも発音形(トーキョー)でも、長音正規化で揃う
    assert align_texts(["トウキョウノソラ"], ["東京の空"]) == [0]
    assert align_texts(["トーキョーノソラ"], ["東京の空"]) == [0]
