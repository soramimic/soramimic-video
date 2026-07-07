from soramimic_video.mora_align import AlignedMora, build_targets, interpolate_missing


def test_build_targets_maps_tokens_to_moras():
    vocab = {"シ": 10, "ズ": 11, "キ": 12, "ャ": 13}
    targets, owners = build_targets([["シ", "ズ"], ["キャ"]], vocab)
    assert targets == [10, 11, 12, 13]
    assert owners == [(0, 0), (0, 1), (1, 0), (1, 0)]


def test_build_targets_falls_back_to_hiragana():
    vocab = {"し": 5}
    targets, owners = build_targets([["シ"]], vocab)
    assert targets == [5]
    assert owners == [(0, 0)]


def test_build_targets_skips_unknown_chars():
    vocab = {"ア": 1}
    targets, owners = build_targets([["ア", "ー"]], vocab)
    assert targets == [1]
    assert owners == [(0, 0)]


def _m(line: int, mora: int, kana: str, start: float, end: float) -> AlignedMora:
    return AlignedMora(
        line=line, mora=mora, kana=kana, start_sec=start, end_sec=end, score=1.0
    )


def test_interpolate_missing_uses_neighbors():
    moras = [
        _m(0, 0, "ア", 1.0, 1.5),
        _m(0, 1, "ー", -1.0, -1.0),  # スパンなし
        _m(0, 2, "イ", 2.0, 2.5),
    ]
    interpolate_missing(moras)
    assert moras[1].start_sec == 1.5
    assert moras[1].end_sec == 2.0


def test_interpolate_missing_at_edges():
    moras = [
        _m(0, 0, "ー", -1.0, -1.0),
        _m(0, 1, "ア", 1.0, 1.5),
        _m(0, 2, "ー", -1.0, -1.0),
    ]
    interpolate_missing(moras)
    assert moras[0].start_sec == 0.0
    assert moras[0].end_sec == 1.0
    assert moras[2].start_sec == 1.5
    assert moras[2].end_sec == 1.5
