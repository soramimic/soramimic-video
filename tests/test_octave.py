from soramimic_video import octave, voicevox
from soramimic_video.octave import (
    NEUTRINO_SAFE_KEY_MAX,
    NEUTRINO_SAFE_KEY_MIN,
    VOICEVOX_SAFE_KEY_MAX,
    VOICEVOX_SAFE_KEY_MIN,
    auto_octave_shift,
)


def _vv(keys, transpose=0):
    return auto_octave_shift(keys, transpose, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX)


def _neu(keys, transpose=0):
    return auto_octave_shift(keys, transpose, NEUTRINO_SAFE_KEY_MIN, NEUTRINO_SAFE_KEY_MAX)


# ---- 一般化した auto_octave_shift(VOICEVOX既定音域で従来と同結果) ----


def test_voicevox_range_matches_legacy_wrapper():
    # 音域引数化しても、VOICEVOX音域を渡せば voicevox.auto_octave_shift と一致する
    for keys in ([], [60, 65, 70, 75], list(range(67, 90)), list(range(40, 60))):
        assert _vv(keys) == voicevox.auto_octave_shift(keys)


def test_voicevox_range_known_values():
    assert _vv(list(range(67, 90))) == -12  # 高すぎ→1オクターブ下
    assert _vv([60, 65, 70, 75]) == 0  # 音域内→シフトなし
    assert _vv(list(range(67, 90)), transpose=-12) == 0  # 既に下げ済み
    assert _vv(list(range(40, 60))) == 12  # 低すぎ→1オクターブ上
    assert _vv([]) == 0


# ---- NEUTRINO音域(MIDI 50〜74) ----


def test_neutrino_high_song_goes_down():
    # C5付近より上(80〜84)は-12で68〜72になりNEUTRINO音域(50〜74)に収まる
    assert _neu([80, 82, 84]) == -12


def test_neutrino_in_range_stays():
    assert _neu([55, 60, 65, 70]) == 0


def test_neutrino_low_song_goes_up():
    # A2付近(40台)は+12で音域に入る
    assert _neu(list(range(38, 50))) == 12


def test_neutrino_considers_user_transpose():
    # ユーザーが既に-12している高音曲は追加調整不要
    assert _neu([80, 82, 84], transpose=-12) == 0


def test_neutrino_empty():
    assert _neu([]) == 0


def test_engine_ranges_are_two_octaves():
    # どちらも2オクターブ幅で設計している(将来モデル別に広げる余地を残す)
    assert VOICEVOX_SAFE_KEY_MAX - VOICEVOX_SAFE_KEY_MIN == 24
    assert NEUTRINO_SAFE_KEY_MAX - NEUTRINO_SAFE_KEY_MIN == 24
    assert octave.NEUTRINO_SAFE_KEY_MIN < octave.NEUTRINO_SAFE_KEY_MAX
