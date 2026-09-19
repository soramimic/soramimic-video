import os

import pytest

pytest.importorskip("MeCab")
pytest.importorskip("unidic_lite")

from soramimic_video.kana import split_moras  # noqa: E402
from soramimic_video.reading import (  # noqa: E402
    automatic_reading_candidates,
    reading_candidates,
    reading_tokens,
    text_to_kana,
    text_to_kana_unidic,
)


def test_automatic_candidates_add_digitwise_reading():
    candidates = automatic_reading_candidates("4443で外れる炭酸水")
    assert candidates[0].startswith("ヨンセンヨンヒャクヨンジューサン")
    assert "ヨンヨンヨンサンデハズレルタンサンスイ" in candidates


def test_automatic_digitwise_candidate_can_win_with_kana_evidence():
    from soramimic_video.kana_whisper import choose_reading

    candidates = automatic_reading_candidates("4443で外れる炭酸水")
    decision = choose_reading(
        candidates,
        ["ヨーヨーヨーダンベラズ", "ヨーヨーヨーゼンベンハズレ"],
    )
    assert candidates[decision.selected_index].startswith("ヨンヨンヨンサン")
    assert decision.reason == "kana-evidence"


def test_automatic_candidates_keep_english_dictionary_first_and_add_spelling():
    candidates = automatic_reading_candidates("reason")
    assert candidates[0] == reading_candidates("reason")[0] == "リーザン"
    assert "アールイーエーエスオーエヌ" in candidates


def test_reading_candidates_include_yomi_connected_english():
    candidates = reading_candidates("did you")
    assert candidates[0] == "ディドユー"
    assert "ディジュー" in candidates


def test_reading_candidates_keep_connected_english_with_fewer_moras():
    candidates = reading_candidates("Shout it out")

    assert candidates[0] == "シャウトイットアウト"
    assert "シャウティタウト" in candidates
    assert len(split_moras("シャウティタウト")) < len(split_moras(candidates[0]))


@pytest.mark.parametrize("candidate_builder", [reading_candidates, automatic_reading_candidates])
def test_compact_english_reading_reaches_acoustic_selection(candidate_builder):
    from soramimic_video.kana_whisper import choose_reading

    candidates = candidate_builder("Shout it out!")

    assert candidates[0] == "シャウトイットアウト"
    assert "シャティタ" in candidates
    decision = choose_reading(candidates, ["シャティタ", "シャティタ"])
    assert candidates[decision.selected_index] == "シャティタ"
    assert decision.reason == "kana-evidence"


def test_repeated_connected_english_prioritizes_mora_count_variants():
    candidates = reading_candidates("Shout it out! Shout it out!")

    assert candidates[0] == "シャウトイットアウトシャウトイットアウト"
    assert "シャティタシャウトイットアウト" in candidates
    assert len(candidates) == 8
    assert len({len(split_moras(candidate)) for candidate in candidates}) == 7
    assert all(not candidate.startswith("エスエイチ") for candidate in candidates)


def test_automatic_candidates_include_yomi_letter_names():
    candidates = automatic_reading_candidates("AI")
    assert candidates[0] == "アイ"
    assert "エーアイ" in candidates


def test_automatic_candidates_filter_japanese_by_vowels_not_length(monkeypatch):
    monkeypatch.setattr(
        "soramimic_video.reading.reading_candidates",
        lambda _text: ["カサ", "ガタ", "キサ", "カサラ"],
    )
    assert automatic_reading_candidates("仮") == ["カサ", "キサ", "カサラ"]


def test_automatic_candidates_add_supported_symbol_reading(monkeypatch):
    monkeypatch.setattr(
        "soramimic_video.reading.reading_candidates", lambda _text: ["タス"]
    )
    assert automatic_reading_candidates("+") == ["タス", "プラス"]
    assert automatic_reading_candidates("＋") == ["タス", "プラス"]


def test_public_yomi_hides_native_dictionary_path(monkeypatch, capfd):
    yomi = pytest.importorskip("soramimic_yomi")
    internal = b"reading /srv/soramimic/private/user.csv ... 1\n"

    def fake_get_yomi(_text):
        os.write(1, internal)
        return "テスト"

    monkeypatch.setattr(yomi, "get_yomi", fake_get_yomi)
    monkeypatch.setenv("SORAMIMIC_PUBLIC", "1")

    from soramimic_video.reading import text_to_kana_yomi

    assert text_to_kana_yomi("テスト") == "テスト"
    captured = capfd.readouterr()
    assert "/srv/soramimic/private" not in captured.out


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


def test_reading_candidates_include_unidic_nbest_pronunciations():
    cands = reading_candidates("あんなに側にいたのに")
    assert cands[0] == "アンナニガワニイタノニ"
    assert "アンナニソバニイタノニ" in cands
    assert len(cands) == 2


def test_reading_candidates_include_nani_for_naniwo():
    cands = reading_candidates("何をしていたの")
    assert any(candidate.startswith("ナニ") for candidate in cands)


def test_reading_candidates_keep_explicit_ruby_across_unidic_nbest():
    cands = reading_candidates("あんなに｜側《そば》にいたのに")
    assert cands
    assert all("ソバニ" in candidate for candidate in cands)
    assert all("ガワニ" not in candidate for candidate in cands)


def test_reading_tokens_uses_pronunciation_for_particles():
    """助詞は発音形(は→ワ、へ→エ)。XFカナ側(xfparse)と揃える必要がある。"""
    pytest.importorskip("soramimic_yomi")
    assert reading_tokens("私は歌う") == [("私", "ワタシ"), ("は", "ワ"), ("歌う", "ウタウ")]
    assert reading_tokens("海へ行く") == [("海", "ウミ"), ("へ", "エ"), ("行く", "イク")]
