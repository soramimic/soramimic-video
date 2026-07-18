from soramimic_video.align import (
    align_texts,
    build_subtitle_segments,
    parse_granularity_override,
    resolve_granularity,
    split_lyric_to_phrases,
)


def test_align_one_to_one():
    xf = ["沈むように", "溶けてゆくように"]
    lyrics = ["沈むように", "溶けてゆくように"]
    assert align_texts(xf, lyrics) == [0, 1]


def test_align_two_xf_lines_to_one_lyric_line():
    xf = ["沈むように", "溶けてゆくように", "二人だけの空が", "広がる夜に"]
    lyrics = ["沈むように 溶けてゆくように", "二人だけの空が 広がる夜に"]
    assert align_texts(xf, lyrics) == [0, 0, 1, 1]
    # 同じ行に割り当たった2つのXF行を、行全文ではなく対応部分へ切り分けられる
    assert split_lyric_to_phrases(
        ["沈むように", "溶けてゆくように"], "沈むように 溶けてゆくように"
    ) == ["沈むように", "溶けてゆくように"]
    assert split_lyric_to_phrases(
        ["二人だけの空が", "広がる夜に"], "二人だけの空が 広がる夜に"
    ) == ["二人だけの空が", "広がる夜に"]


def test_split_lyric_single_and_boundaries():
    # XF行が1つなら行全文がそのまま
    assert split_lyric_to_phrases(["沈むように溶けてゆくように"], "沈むように溶けてゆくように") == [
        "沈むように溶けてゆくように"
    ]
    # 空白なしで連結しても各行に割れる(連結すると元の行に戻る)
    pieces = split_lyric_to_phrases(["沈む", "ように", "溶けて"], "沈むように溶けて")
    assert pieces == ["沈む", "ように", "溶けて"]
    assert "".join(pieces) == "沈むように溶けて"


def test_split_lyric_falls_back_to_proportional():
    # 表記がまったく重ならない(カナ×漢字)ときは文字数比の按分。空文字は作らない
    pieces = split_lyric_to_phrases(["ゲッコウ", "シズムヨウニ"], "月光 沈むように")
    assert len(pieces) == 2
    assert all(p for p in pieces)
    assert "".join(p.replace(" ", "") for p in pieces) == "月光沈むように".replace(" ", "")


def test_resolve_granularity_precedence():
    # 要素の指定 > override > source既定
    assert resolve_granularity("original", None, None) == "line"
    assert resolve_granularity("parody", None, None) == "phrase"
    assert resolve_granularity("original", "phrase", None) == "phrase"
    assert resolve_granularity("original", None, {"original": "phrase"}) == "phrase"
    assert resolve_granularity("original", "line", {"original": "phrase"}) == "line"  # 要素優先


def test_parse_granularity_override():
    assert parse_granularity_override("parody:line|original:phrase") == {
        "parody": "line", "original": "phrase"
    }
    assert parse_granularity_override("") is None
    assert parse_granularity_override("bogus|parody:nope") is None
    assert parse_granularity_override("parody:line|junk") == {"parody": "line"}


def test_build_subtitle_segments_original_line_merges_group():
    # 連続する同一元歌詞行は1枚に畳まれ、通しタイミングになる(チラつき防止)
    originals = ["沈むように 溶けてゆくように", "沈むように 溶けてゆくように"]
    spans = [(0.0, 1.0), (1.0, 2.0)]
    segs = build_subtitle_segments(
        "original", "line", originals, originals, ["沈むように", "溶けてゆくように"], spans
    )
    assert len(segs) == 1
    assert segs[0].text == "沈むように 溶けてゆくように"
    assert (segs[0].start, segs[0].end) == (0.0, 2.0)
    assert segs[0].indices == [0, 1]


def test_build_subtitle_segments_original_phrase_splits():
    originals = ["沈むように 溶けてゆくように", "沈むように 溶けてゆくように"]
    spans = [(0.0, 1.0), (1.0, 2.0)]
    segs = build_subtitle_segments(
        "original", "phrase", originals, originals, ["沈むように", "溶けてゆくように"], spans
    )
    assert [s.text for s in segs] == ["沈むように", "溶けてゆくように"]
    assert [(s.start, s.end) for s in segs] == [(0.0, 1.0), (1.0, 2.0)]


def test_build_subtitle_segments_parody_line_concatenates():
    originals = ["同じ行", "同じ行"]
    parody_full = ["静", "川"]
    spans = [(0.0, 1.0), (1.0, 2.0)]
    segs = build_subtitle_segments(
        "parody", "line", originals, parody_full, ["a", "b"], spans, sep="  "
    )
    assert len(segs) == 1
    assert segs[0].text == "静  川"
    assert (segs[0].start, segs[0].end) == (0.0, 2.0)


def test_build_subtitle_segments_none_never_merges():
    # 未対応(None)行は隣と結合せず、それぞれ独立して出る
    originals = [None, None]
    full = ["あ", "い"]
    spans = [(0.0, 1.0), (1.0, 2.0)]
    segs = build_subtitle_segments("original", "line", originals, full, ["あ", "い"], spans)
    assert [s.text for s in segs] == ["あ", "い"]


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
