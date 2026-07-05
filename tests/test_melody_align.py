import pytest

from soramimic_video.melody_align import (
    MelodyNote,
    assemble_mora_notes,
    channel_match_score,
    estimate_transpose,
    fit_global_map,
    match_moras_to_notes,
    monophony_ratio,
    skyline,
)
from soramimic_video.mora_align import AlignedMora


def _note(start: float, end: float, pitch: int) -> MelodyNote:
    return MelodyNote(start_sec=start, end_sec=end, midi_note=pitch)


def _mora(i: int, kana: str, start: float, end: float) -> AlignedMora:
    return AlignedMora(line=0, mora=i, kana=kana, start_sec=start, end_sec=end, score=1.0)


def _spans(onsets: list[float], dur: float = 0.4) -> list[tuple[float, float]]:
    return [(t, t + dur) for t in onsets]


def test_monophony_ratio():
    mono = [_note(0, 1, 60), _note(1, 2, 62), _note(2, 3, 64)]
    poly = [_note(0, 2, 60), _note(1, 3, 64), _note(2, 4, 67)]
    assert monophony_ratio(mono) == 1.0
    assert monophony_ratio(poly) == 0.0


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


def test_fit_global_map_recovers_offset_and_scale():
    # 音符列を scale=1.02, offset=+3.4s で写すとモーラ列に一致する
    note_onsets = [float(i) for i in range(40)]
    mora_onsets = [1.02 * t + 3.4 for t in note_onsets]
    scale, offset = fit_global_map(mora_onsets, note_onsets)
    assert scale == pytest.approx(1.02, abs=0.011)
    assert offset == pytest.approx(3.4, abs=0.3)
    # 写像後の残差が小さいこと(格子の分解能内)
    residuals = [
        abs(scale * t + offset - y)
        for t, y in zip(note_onsets, mora_onsets, strict=True)
    ]
    assert max(residuals) < 0.15


def test_fit_global_map_negative_offset():
    note_onsets = [5.0 + i * 0.5 for i in range(60)]
    mora_onsets = [t - 4.1 for t in note_onsets]
    scale, offset = fit_global_map(mora_onsets, note_onsets)
    assert scale == pytest.approx(1.0, abs=0.011)
    assert offset == pytest.approx(-4.1, abs=0.15)


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


def test_match_moras_to_notes_exact():
    pairs = match_moras_to_notes([1.0, 2.0, 3.0], _spans([1.05, 1.95, 3.1]))
    assert pairs == [(0, 0), (1, 1), (2, 2)]


def test_match_moras_to_notes_extra_note():
    # 2.5sの音符はモーラに対応しない(メリスマ or 間奏)
    pairs = match_moras_to_notes([1.0, 2.0, 3.0], _spans([1.0, 2.0, 2.5, 3.0]))
    assert (None, 2) in pairs
    assert (0, 0) in pairs and (2, 3) in pairs


def test_match_moras_to_notes_shares_note_for_repeated_moras():
    # MIDIが同音連打を1音符にまとめた箇所: 3モーラが1音符を共有し、
    # 次のモーラは次の音符にずれずに対応する
    moras = [1.0, 1.2, 1.4, 2.0]
    notes = [(1.0, 1.6), (2.0, 2.4)]
    pairs = match_moras_to_notes(moras, notes)
    assert pairs == [(0, 0), (1, 0), (2, 0), (3, 1)]


def test_match_moras_to_notes_extra_mora():
    # 遠く離れた孤立モーラは音符共有(区間外は距離ペナルティ)より
    # skip(フォールバック)が選ばれる
    pairs = match_moras_to_notes([1.0, 5.0, 9.0], _spans([1.0, 9.0]))
    assert (1, None) in pairs


def test_match_moras_to_notes_pitch_cost_disambiguates():
    # 時刻だけでは近い方の音符を選んでしまうケースを、音高で正しく選ぶ。
    # f0=72に対し、時刻の近い方(65)でなく音高の合う方(72)へ
    moras = [1.05]
    spans = [(1.0, 1.15), (1.2, 1.8)]
    pitches = [65, 72]
    no_pitch = match_moras_to_notes(moras, spans)
    with_pitch = match_moras_to_notes(
        moras, spans, mora_pitches=[72], note_pitches=pitches, transpose=0
    )
    assert (0, 0) in no_pitch  # 時刻のみ: 近い方を選んでしまう
    assert (0, 1) in with_pitch  # 音高込み: 正しい音符を選ぶ


def test_estimate_transpose_octave():
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (1, 1), (2, None)]
    fallback = [72, 74, 60]  # f0はオク上で歌っている
    notes = [_note(0, 1, 60), _note(1, 2, 62)]
    assert estimate_transpose(pairs, fallback, notes) == 12


def test_assemble_matched_uses_midi_pitch_and_timing():
    # タイミングは楽譜基準: CTC(1.0)でなくMIDIのnote-on/off(1.1-1.8)を使う
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.1, 1.8, 67)]
    result = assemble_mora_notes(aligned, notes, [(0, 0)], fallback_midi=[60])
    assert result[0].midi_note == 67
    assert result[0].start_sec == 1.1
    assert result[0].end_sec == 1.8


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


def test_assemble_drops_overlapping_chord_note():
    # マッチ音符と同時に鳴る和音の余り → メリスマにしない
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.0, 1.8, 67), _note(1.0, 1.8, 60)]
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (None, 1)]
    result = assemble_mora_notes(aligned, notes, pairs, fallback_midi=[60])
    assert [m.kana for m in result] == ["ア"]


def test_assemble_unmatched_mora_falls_back_to_f0():
    aligned = [_mora(0, "ア", 1.0, 1.4)]
    result = assemble_mora_notes(aligned, [], [(0, None)], fallback_midi=[63])
    assert result[0].midi_note == 63
    assert result[0].start_sec == 1.0


def test_assemble_shared_note_splits_at_ctc_onsets():
    # 同音連打3モーラが1音符を共有: 先頭は音符開始、以降の分割位置はCTC時刻
    aligned = [_mora(0, "シ", 0.9, 1.1), _mora(1, "ズ", 1.2, 1.3), _mora(2, "ム", 1.4, 1.5)]
    notes = [_note(1.0, 1.6, 70)]
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (1, 0), (2, 0)]
    result = assemble_mora_notes(aligned, notes, pairs, fallback_midi=[60, 60, 60])
    assert [m.midi_note for m in result] == [70, 70, 70]
    assert [m.start_sec for m in result] == [1.0, 1.2, 1.4]  # 先頭はMIDI開始に吸着


def test_assemble_legato_closes_small_gaps():
    # MIDIゲート由来の小さい隙間(0.1s)は接続、大きい隙間(0.5s)は残す
    aligned = [_mora(0, "ア", 1.0, 1.2), _mora(1, "イ", 1.5, 1.7), _mora(2, "ウ", 2.4, 2.6)]
    notes = [_note(1.0, 1.4, 60), _note(1.5, 1.9, 62), _note(2.4, 2.8, 64)]
    pairs: list[tuple[int | None, int | None]] = [(0, 0), (1, 1), (2, 2)]
    result = assemble_mora_notes(aligned, notes, pairs, fallback_midi=[60, 62, 64])
    assert result[0].end_sec == 1.5  # 0.1sの隙間は接続
    assert result[1].end_sec == 1.9  # 0.5sの隙間は残す


def test_assemble_applies_transpose():
    aligned = [_mora(0, "ア", 1.0, 1.2)]
    notes = [_note(1.0, 1.5, 55)]
    result = assemble_mora_notes(aligned, notes, [(0, 0)], fallback_midi=[67], transpose=12)
    assert result[0].midi_note == 67
