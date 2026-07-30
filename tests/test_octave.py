from soramimic_video import octave, voicevox
from soramimic_video.octave import (
    NEUTRINO_SAFE_KEY_MAX,
    NEUTRINO_SAFE_KEY_MIN,
    VOICEVOX_SAFE_KEY_MAX,
    VOICEVOX_SAFE_KEY_MIN,
    auto_octave_shift,
    auto_shift_plan,
    resolve_auto_shift,
)
from soramimic_video.project import Project, SongInfo


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


# ---- 曲全体キー変更(カラオケ方式)を含む auto_shift_plan ----

# 音域の広い実曲の例(女々しくて: メロディ 61〜83)。どのオクターブでも
# VOICEVOX安全音域54〜78に収まらないが、-5半音で全音符が収まる
WIDE_KEYS = list(range(61, 84))


def _vv_plan(keys, transpose=0, **kw):
    return auto_shift_plan(
        keys, transpose, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, **kw
    )


def _neu_plan(keys, transpose=0, **kw):
    return auto_shift_plan(
        keys, transpose, NEUTRINO_SAFE_KEY_MIN, NEUTRINO_SAFE_KEY_MAX, **kw
    )


def test_plan_keeps_key_when_octave_fits():
    """オクターブだけで収まる曲はキー変更0(=既存曲の挙動は不変)。"""
    for keys in ([], [60, 65, 70, 75], list(range(67, 90)), list(range(45, 60))):
        plan = _vv_plan(keys)
        assert plan.key_shift == 0
        assert plan.out_of_range == 0
        # 歌の移調量も従来のオクターブ調整と一致する
        assert plan.vocal_shift == _vv(keys)


def test_plan_key_changes_only_when_octave_cannot_fit():
    plan = _vv_plan(WIDE_KEYS)
    assert plan.out_of_range == 0
    # -5半音で 56〜78。キー変更は最小(|k|=5)で、オクターブは動かさない
    assert (plan.vocal_shift, plan.key_shift) == (-5, -5)
    # 従来のオクターブのみでは収まりきらないことの確認(この曲だから効く)
    assert _vv_plan(WIDE_KEYS, max_key_shift=0).out_of_range > 0


def test_plan_key_change_for_low_wide_song():
    """低くて広い曲(40〜59)も、+1オクターブ+2半音で全音符が収まる。"""
    keys = list(range(40, 60))
    assert _vv_plan(keys, max_key_shift=0).out_of_range == 2  # 従来は下2音がはみ出す
    plan = _vv_plan(keys)
    assert (plan.vocal_shift, plan.key_shift, plan.out_of_range) == (14, 2, 0)


def test_plan_combines_key_change_and_octave():
    """NEUTRINO音域(50〜74)では -11半音、すなわち +1半音 & -1オクターブが最小。"""
    plan = _neu_plan(WIDE_KEYS)
    assert plan.out_of_range == 0
    assert (plan.vocal_shift, plan.key_shift) == (-11, 1)
    assert min(WIDE_KEYS) + plan.vocal_shift >= NEUTRINO_SAFE_KEY_MIN
    assert max(WIDE_KEYS) + plan.vocal_shift <= NEUTRINO_SAFE_KEY_MAX


def test_plan_considers_user_transpose():
    """ユーザーが+12している広音域曲は、その上で音域に収まるシフトを選ぶ。"""
    plan = _vv_plan(WIDE_KEYS, transpose=12)
    assert plan.out_of_range == 0
    assert all(
        VOICEVOX_SAFE_KEY_MIN <= k + 12 + plan.vocal_shift <= VOICEVOX_SAFE_KEY_MAX
        for k in WIDE_KEYS
    )


def test_plan_survives_impossible_range():
    """3オクターブ超の極端な曲はどうやっても収まらないが、最善を返して壊れない。"""
    keys = list(range(40, 101))
    plan = _vv_plan(keys)
    assert plan.out_of_range > 0
    assert abs(plan.key_shift) <= octave.MAX_KEY_SHIFT
    # 範囲外の数は実際にそのシフトを当てたときの数と一致する
    assert plan.out_of_range == sum(
        1
        for k in keys
        if not VOICEVOX_SAFE_KEY_MIN <= k + plan.vocal_shift <= VOICEVOX_SAFE_KEY_MAX
    )
    # キー変更なしより悪くはならない
    assert plan.out_of_range <= _vv_plan(keys, max_key_shift=0).out_of_range


def test_plan_max_key_shift_zero_is_octave_only():
    plan = _vv_plan(WIDE_KEYS, max_key_shift=0)
    assert plan.key_shift == 0
    assert plan.vocal_shift == _vv(WIDE_KEYS)


def test_auto_octave_shift_never_changes_key():
    """旧APIはオクターブ専用のまま(伴奏を移調できない呼び出し元のため)。"""
    assert _vv(WIDE_KEYS) % 12 == 0


# ---- resolve_auto_shift(projectへのキー変更記録) ----


def _project(**song_kw) -> Project:
    song = SongInfo(midi_path="song.mid", ticks_per_beat=480, **song_kw)
    return Project(song=song)


def test_resolve_records_key_shift_on_project():
    project = _project()
    shift = resolve_auto_shift(
        project, WIDE_KEYS, 0, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, "VOICEVOX"
    )
    assert shift == -5
    assert project.song.key_shift == -5


def test_resolve_resets_key_shift_when_octave_fits():
    project = _project()
    project.song.key_shift = -5  # 前回実行の残り
    shift = resolve_auto_shift(
        project, [80, 82, 84], 0, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, "VOICEVOX"
    )
    assert shift == -12
    assert project.song.key_shift == 0


def test_resolve_skips_key_shift_for_rendered_accompaniment():
    """伴奏がwav(音源分離・プリレンダ)のプロジェクトは移調できないのでキー変更しない。"""
    project = _project(accompaniment_path="no_vocals.wav")
    shift = resolve_auto_shift(
        project, WIDE_KEYS, 0, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, "VOICEVOX"
    )
    assert project.song.key_shift == 0
    assert shift % 12 == 0  # 従来どおりオクターブ単位のみ


def test_resolve_skips_key_shift_without_midi():
    project = Project(song=SongInfo(midi_path="", ticks_per_beat=480))
    resolve_auto_shift(
        project, WIDE_KEYS, 0, VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, "VOICEVOX"
    )
    assert project.song.key_shift == 0


def test_resolve_logs_key_change(caplog):
    project = _project()
    with caplog.at_level("INFO", logger="soramimic_video.octave"):
        resolve_auto_shift(
            project, WIDE_KEYS, 0,
            VOICEVOX_SAFE_KEY_MIN, VOICEVOX_SAFE_KEY_MAX, "VOICEVOX",
        )
    assert any("キー変更" in r.getMessage() for r in caplog.records)
