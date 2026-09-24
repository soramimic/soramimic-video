from soramimic_video.audio_melody import MelodyNote
from soramimic_video.stage3 import _snap_whisper_windows_to_sheetsage_rests


def test_fixed_supplied_reading_does_not_expand_repetitions(monkeypatch):
    from soramimic_video import mora_align
    from soramimic_video.stage3 import build_stage3_layers

    calls = []
    monkeypatch.setattr(mora_align, "decode_repeated_mora_reattacks",
                        lambda *_: calls.append(True) or [])
    _, score = build_stage3_layers(
        ["らら"], ["ララ"],
        [mora_align.AlignedMora(0, 0, "ラ", .05, .15, .9),
         mora_align.AlignedMora(0, 1, "ラ", .3, .4, .9)],
        [MelodyNote(0, .25, 60), MelodyNote(.25, .5, 62)],
        whisper_line_windows=[(0, .5)], enable_repeated_vocalization=True,
        ctc_emissions=object(), fixed_reading_indices=frozenset({0}),
    )
    assert not calls
    assert [slot.kana for slot in score.synthesis_plan] == ["ラ", "ラ"]


def test_score_library_runs_without_a_legacy_pipeline_installation(monkeypatch):
    import builtins

    from soramimic_video.analyze_audio import _require_audio_pipeline
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.stage3 import build_stage3_layers

    original_import = builtins.__import__

    def no_legacy(name, *args, **kwargs):
        if name == "wav_to_xf" or name.startswith("wav_to_xf."):
            raise ImportError("legacy pipeline must not be used")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_legacy)
    _require_audio_pipeline()
    observations, score = build_stage3_layers(
        ["空"], ["ソラ"],
        [AlignedMora(0, 0, "ソ", .05, .15, .8), AlignedMora(0, 1, "ラ", .3, .4, .8)],
        [MelodyNote(0, .25, 60), MelodyNote(.25, .5, 62)],
    )
    assert type(observations).__module__ == "soramimic_score.ir"
    assert [slot.kana for slot in score.synthesis_plan] == ["ソ", "ラ"]
    assert not score.unresolved_unit_ids


def test_whisper_windows_snap_to_nearest_eligible_sheetsage_rests():
    windows = [(0.0, 1.0), (1.1, 2.0), (2.1, 3.0)]
    notes = [
        MelodyNote(0.1, 0.8, 60),
        MelodyNote(1.0, 1.5, 62),
        MelodyNote(1.55, 2.0, 64),
        MelodyNote(2.2, 2.8, 65),
    ]

    assert _snap_whisper_windows_to_sheetsage_rests(windows, notes) == (
        (0.0, 0.9),
        (0.9, 2.1),
        (2.1, 3.0),
    )


def test_whisper_windows_ignore_short_or_distant_sheetsage_rests():
    windows = [(0.0, 1.0), (1.1, 2.0)]
    notes = [
        MelodyNote(0.0, 0.50, 60),
        MelodyNote(0.57, 0.90, 62),
        MelodyNote(3.0, 3.3, 64),
    ]

    assert _snap_whisper_windows_to_sheetsage_rests(windows, notes) == tuple(windows)
