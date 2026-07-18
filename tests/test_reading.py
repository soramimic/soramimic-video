import pytest

pytest.importorskip("MeCab")
pytest.importorskip("unidic_lite")

from soramimic_video.reading import (  # noqa: E402
    reading_candidates,
    reading_tokens,
    text_to_kana,
    text_to_kana_unidic,
)


def test_reading_tokens_surface_and_reading():
    pytest.importorskip("soramimic_yomi")
    tokens = reading_tokens("二人だけの空が広がる夜に")
    # 表層を連結すると元の行に戻る(位置写像の前提)
    assert "".join(surf for surf, _ in tokens) == "二人だけの空が広がる夜に"
    # 各トークンにカナ読みが付く(記号以外)
    surfaces = [surf for surf, _ in tokens]
    assert "広がる" in surfaces
    reading_of = dict(tokens)
    assert reading_of["二人"] == "フタリ"
    assert reading_of["広がる"] == "ヒロガル"


def test_unidic_pron_style():
    # 発音形: 長音はー、助詞は→ワ
    assert text_to_kana_unidic("東京") == "トーキョー"
    assert text_to_kana_unidic("広がって") == "ヒロガッテ"


def test_unidic_futari():
    # ipadicは「二人」を「ニニン」と誤読していた(unidic/yomiに切り替えた理由)
    assert text_to_kana_unidic("二人だけの空が広がる夜に") == "フタリダケノソラガヒロガルヨルニ"


def test_text_to_kana_keeps_kana_oov():
    assert "ラ" in text_to_kana("ラララ")


def test_text_to_kana_drops_symbols():
    assert text_to_kana_unidic("あ、い!") == "アイ"


def test_reading_candidates_dedupes_by_long_vowel_normalization():
    # yomi(ヨー式)とunidic(こちらも発音形)が実質同じ読みなら候補は1つ
    cands = reading_candidates("沈むように")
    normalized = {c.replace("ヨウ", "ヨー") for c in cands}
    assert len(normalized) == len(cands)  # 正規化後に重複しない
    assert all("シズム" in c for c in cands)


def test_reading_candidates_nonempty_first():
    cands = reading_candidates("夜に駆ける")
    assert cands and cands[0]
