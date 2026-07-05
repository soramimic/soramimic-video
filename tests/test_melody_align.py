import pytest

np = pytest.importorskip("numpy")

from soramimic_video.melody_align import (  # noqa: E402
    MelodyNote,
    assemble_mora_notes,
    build_time_map,
    channel_match_score,
    estimate_transpose,
    fill_silent_frames,
    match_moras_to_notes,
    midi_chroma,
    monophony_ratio,
    skyline,
    warp_sec,
)
from soramimic_video.mora_align import AlignedMora  # noqa: E402


def _note(start: float, end: float, pitch: int) -> MelodyNote:
    return MelodyNote(start_sec=start, end_sec=end, midi_note=pitch)


def _mora(i: int, kana: str, start: float, end: float) -> AlignedMora:
    return AlignedMora(line=0, mora=i, kana=kana, start_sec=start, end_sec=end, score=1.0)


def test_monophony_ratio():
    mono = [_note(0, 1, 60), _note(1, 2, 62), _note(2, 3, 64)]
    poly = [_note(0, 2, 60), _note(1, 3, 64), _note(2, 4, 67)]
    assert monophony_ratio(mono) == 1.0
    assert monophony_ratio(poly) == 0.0


def test_channel_match_score_prefers_melody_over_bass():
    # モーラのf0は65前後で輪郭が動く。メロディ(オク上,輪郭一致)がベース(輪郭無関係)に勝つ
    contour = [65, 67, 69, 67, 65, 62, 65, 69]
    fallback = list(contour)
    melody = [_note(i * 0.5, i * 0.5 + 0.4, p + 12) for i, p in enumerate(contour)]
    bass = [_note(i * 0.5, i * 0.5 + 0.4, 40) for i in range(len(contour))]
    pairs: list[tuple[int | None, int | None]] = [(i, i) for i in range(len(contour))]
    mel_score, mel_cov, mel_mad = channel_match_score(pairs, len(contour), fallback, melody)
    bass_score, _, bass_mad = channel_match_score(pairs, len(contour), fallback, bass)
    assert mel_cov == 1.0
    assert mel_mad == 0.0  # 一定オフセット(+12)は輪郭一致とみなす
    assert bass_mad > 0.0
    assert mel_score > bass_score


def test_channel_match_score_no_match():
    score, coverage, _ = channel_match_score([(0, None)], 1, [60], [])
    assert score == -1.0
    assert coverage == 0.0


def test_skyline_keeps_top_voice():
    # C4-E4-G4の和音+旋律C5 → C5だけ残る
    notes = [
        _note(0.0, 1.0, 60),
        _note(0.0, 1.0, 64),
        _note(0.0, 1.0, 67),
        _note(0.0, 1.0, 72),
    ]
    result = skyline(notes)
    assert [n.midi_note for n in result] == [72]


def test_skyline_truncates_when_higher_note_enters():
    notes = [_note(0.0, 2.0, 60), _note(1.0, 2.0, 67)]
    result = skyline(notes)
    assert [(n.start_sec, n.end_sec, n.midi_note) for n in result] == [
        (0.0, 1.0, 60),
        (1.0, 2.0, 67),
    ]


def test_skyline_keeps_sequential_notes():
    notes = [_note(0.0, 1.0, 60), _note(1.0, 2.0, 62), _note(2.0, 3.0, 64)]
    assert len(skyline(notes)) == 3


def test_assemble_drops_overlapping_chord_note():
    # マッチ音符と同時に鳴る和音の余り → メリスマにしない
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.0, 1.8, 67), _note(1.0, 1.8, 60)]
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (None, 1)]
    result = assemble_mora_notes(aligned, notes, pairs, fallback_midi=[60])
    assert [m.kana for m in result] == ["ア"]


def test_midi_chroma_pitch_class_and_norm():
    chroma = midi_chroma([_note(0.0, 1.0, 60)], n_frames=10, frame_sec=0.2)  # C4
    assert chroma.shape == (12, 10)
    assert chroma[0, 0] == pytest.approx(1.0)  # C行が立つ(正規化済み)
    assert chroma[:, 9].sum() == 0  # ノートの無いフレームは0


def test_fill_silent_frames():
    chroma = midi_chroma([_note(0.0, 1.0, 60)], n_frames=10, frame_sec=0.2)
    filled = fill_silent_frames(chroma)
    assert filled[:, 9] == pytest.approx(np.full(12, 1 / np.sqrt(12)))  # 無音は一様に
    assert filled[0, 0] == pytest.approx(1.0)  # 有音フレームはそのまま


def test_build_time_map_and_warp():
    # midiフレームiがaudioフレーム2iに対応する経路(2倍に間延びした演奏)
    wp = np.array([[i, 2 * i] for i in range(10)])
    midi_t, audio_t = build_time_map(wp, frame_sec=0.5)
    assert warp_sec(1.0, midi_t, audio_t) == pytest.approx(2.0)
    assert warp_sec(2.25, midi_t, audio_t) == pytest.approx(4.5)


def test_match_moras_to_notes_exact():
    pairs = match_moras_to_notes([1.0, 2.0, 3.0], [1.05, 1.95, 3.1])
    assert pairs == [(0, 0), (1, 1), (2, 2)]


def test_match_moras_to_notes_extra_note():
    # 2.5sの音符はモーラに対応しない(メリスマ or 間奏)
    pairs = match_moras_to_notes([1.0, 2.0, 3.0], [1.0, 2.0, 2.5, 3.0])
    assert (None, 2) in pairs
    assert (0, 0) in pairs and (2, 3) in pairs


def test_match_moras_to_notes_extra_mora():
    pairs = match_moras_to_notes([1.0, 5.0, 9.0], [1.0, 9.0])
    assert (1, None) in pairs


def test_estimate_transpose_octave():
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (1, 1), (2, None)]
    fallback = [72, 74, 60]  # f0はオク上で歌っている
    notes = [_note(0, 1, 60), _note(1, 2, 62)]
    assert estimate_transpose(pairs, fallback, notes) == 12


def test_assemble_matched_uses_midi_pitch_and_ctc_onset():
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.1, 1.8, 67)]  # CTCと0.1s差 → CTC開始を採用
    result = assemble_mora_notes(aligned, notes, [(0, 0)], fallback_midi=[60])
    assert result[0].midi_note == 67
    assert result[0].start_sec == 1.0
    assert result[0].end_sec == 1.8  # 終端はMIDIのnote-off


def test_assemble_far_onset_uses_midi_onset():
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(2.0, 2.5, 67)]  # 0.3s超の差 → MIDI開始を信用
    result = assemble_mora_notes(aligned, notes, [(0, 0)], fallback_midi=[60])
    assert result[0].start_sec == 2.0


def test_assemble_melisma_and_interlude():
    aligned = [_mora(0, "ア", 1.0, 1.2), _mora(1, "イ", 3.0, 3.3)]
    notes = [
        _note(1.0, 1.5, 60),
        _note(1.6, 2.0, 64),  # 直前に間近で続く余り音符 → メリスマ「ー」
        _note(10.0, 11.0, 70),  # 離れた余り音符 → 間奏として破棄
        _note(3.0, 3.5, 65),
    ]
    pairs = [(0, 0), (None, 1), (None, 2), (1, 3)]
    result = assemble_mora_notes(aligned, notes, pairs, fallback_midi=[60, 62])
    assert [m.kana for m in result] == ["ア", "ー", "イ"]
    assert result[1].midi_note == 64
    assert result[1].line == 0


def test_assemble_unmatched_mora_falls_back_to_f0():
    aligned = [_mora(0, "ア", 1.0, 1.4)]
    result = assemble_mora_notes(aligned, [], [(0, None)], fallback_midi=[63])
    assert result[0].midi_note == 63
    assert result[0].start_sec == 1.0


def test_assemble_applies_transpose():
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.0, 1.5, 55)]
    result = assemble_mora_notes(aligned, notes, [(0, 0)], fallback_midi=[67], transpose=12)
    assert result[0].midi_note == 67
