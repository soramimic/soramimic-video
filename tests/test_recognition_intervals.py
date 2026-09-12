from soramimic_video.lyric_recognition import _segment_pass_id


def test_overlapping_segments_remain_in_separate_compatible_streams():
    intervals = [(0, 2), (1.5, 3), (1.7, 2.7), (2, 4), (3, 5), (5, 6)]
    ends: list[float] = []
    streams: dict[str, list[tuple[float, float]]] = {}
    assignments = []
    for start, end in intervals:
        stream = _segment_pass_id("pass", ends, start, end)
        assignments.append(stream)
        streams.setdefault(stream, []).append((start, end))
    assert assignments == [
        "pass", "pass:overlap-1", "pass:overlap-2", "pass", "pass:overlap-1", "pass",
    ]
    assert sum(map(len, streams.values())) == len(intervals)
    assert all(a[1] <= b[0] for values in streams.values()
               for a, b in zip(values, values[1:], strict=False))


def test_adjacent_nonoverlapping_segments_keep_the_original_pass():
    ends: list[float] = []
    assert [_segment_pass_id("pass", ends, a, b)
            for a, b in [(0, 1), (1, 2), (3, 4)]] == ["pass"] * 3
