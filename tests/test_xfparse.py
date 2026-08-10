from pathlib import Path

from helpers import build_xf_midi
from soramimic_video import reading as reading_mod
from soramimic_video.xfparse import analyze_midi, normalize_kana, parse_lyric_events


def test_parse_lyric_events_brackets_and_breaks():
    events = [
        (0, "<"),
        (0, "沈[し"),
        (240, "ず]"),
        (480, "む"),
        (720, "/"),
        (740, "溶[と]"),
        (960, "け"),
    ]
    moras = parse_lyric_events(events)
    assert [(m.surface, m.kana) for m in moras] == [
        ("沈", "し"),
        ("", "ず"),
        ("む", "む"),
        ("溶", "と"),
        ("け", "け"),
    ]
    assert moras[0].line_break_before is True  # '<'
    assert moras[3].line_break_before is True  # '/'
    assert moras[1].line_break_before is False


def test_parse_lyric_events_leading_break_in_same_event():
    moras = parse_lyric_events([(0, "あ"), (480, "/い")])
    assert moras[1].line_break_before is True
    assert moras[1].kana == "い"


def test_parse_lyric_events_repairs_broken_word_internal_breaks():
    """「止め/る」を1モーラ行にせず、後続の異常な重複表層も除く。"""
    moras = parse_lyric_events([
        (0, "/止[と]"), (240, "め"), (480, "/る"),
        (720, "/る[ほ]"), (960, "ほ"), (1200, "ど"),
        (1440, "の"), (1680, "い"), (1920, "意思[い"), (2160, "し]"),
    ])

    assert [m.line_break_before for m in moras] == [True] + [False] * 9
    assert [(m.surface, m.kana) for m in moras] == [
        ("止", "と"), ("め", "め"), ("る", "る"),
        ("", "ー"), ("ほ", "ほ"), ("ど", "ど"),
        ("の", "の"), ("意思", "い"), ("", "ー"), ("", "し"),
    ]


def test_parse_lyric_events_keeps_legitimate_repeated_short_lines():
    moras = parse_lyric_events([(0, "/あ"), (480, "/あ")])
    assert [m.line_break_before for m in moras] == [True, True]
    assert [m.surface for m in moras] == ["あ", "あ"]


def test_parse_lyric_events_does_not_repair_unanchored_long_vowel_shape():
    moras = parse_lyric_events([(0, "青[あ]"), (240, "お"), (480, "い"),
                                (720, "色[い"), (960, "ろ]")])
    assert [(m.surface, m.kana) for m in moras] == [
        ("青", "あ"), ("お", "お"), ("い", "い"), ("色", "い"), ("", "ろ")
    ]


def test_normalize_kana():
    assert normalize_kana("しズ") == "シズ"
    assert normalize_kana("キャー!") == "キャー"


def test_analyze_midi_basic(tmp_path: Path):
    # 2行: 「沈[しず]む」(3音符) / 「とけ」(2音符)
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 240, 64), (1440, 240, 65), (1680, 240, 67)],
        lyric_events=[
            (480, "沈[し"),
            (720, "ず]"),
            (960, "む"),
            (1440, "/と"),
            (1680, "け"),
        ],
    )
    project = analyze_midi(midi)
    assert [n.kana for n in project.notes] == ["シ", "ズ", "ム", "ト", "ケ"]
    assert [n.midi_note for n in project.notes] == [60, 62, 64, 65, 67]
    assert len(project.lines) == 2
    assert project.lines[0].xf_surface == "沈む"
    assert project.lines[0].xf_kana == "シズム"
    assert project.lines[1].note_ids == [3, 4]
    # tempo 500000us/beat, 480tpb -> 1tick = 1/960秒
    assert abs(project.notes[0].start_sec - 0.5) < 1e-6
    assert abs(project.notes[0].end_sec - 0.75) < 1e-6


def test_analyze_midi_merges_broken_word_internal_xf_lines(tmp_path: Path):
    midi = build_xf_midi(
        tmp_path / "broken-break.mid",
        notes=[(tick, 240, 60 + i) for i, tick in enumerate(range(0, 2400, 240))],
        lyric_events=[
            (0, "/止[と]"), (240, "め"), (480, "/る"),
            (720, "/る[ほ]"), (960, "ほ"), (1200, "ど"),
            (1440, "の"), (1680, "い"), (1920, "意思[い"), (2160, "し]"),
        ],
    )

    project = analyze_midi(midi)

    assert len(project.lines) == 1
    assert project.lines[0].xf_surface == "止めるほどの意思"
    assert project.lines[0].xf_kana == "トメルーホドノイーシ"
    assert [note.line for note in project.notes] == [0] * 10
    assert project.notes[3].surface == ""  # 後行先頭の重複した「る」
    assert project.notes[7].surface == "意思"
    assert project.notes[8].kana == "ー"


def test_analyze_midi_multi_mora_note(tmp_path: Path):
    # 「らい」が1音符に載る(2つ目の歌詞イベントに音符がない)
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(0, 240, 60), (240, 480, 62)],
        lyric_events=[(0, "き"), (240, "ら"), (480, "い")],
    )
    project = analyze_midi(midi)
    assert len(project.notes) == 2
    assert project.notes[1].kana == "ライ"


def test_analyze_midi_fills_kanji_without_ruby(tmp_path: Path, monkeypatch):
    midi = build_xf_midi(
        tmp_path / "missing-ruby.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64)],
        lyric_events=[(0, "<僕"), (240, "の"), (480, "事")],
    )
    monkeypatch.setattr(reading_mod, "text_to_kana", lambda _text: "ボクノコト")

    project = analyze_midi(midi)

    assert [n.kana for n in project.notes] == ["ボク", "ノ", "コト"]
    assert project.lines[0].xf_kana == "ボクノコト"


def test_analyze_midi_fixes_particle_reading(tmp_path: Path):
    """助詞の「は」はXFでは表記どおり(ハ)なので、読みを発音形(ワ)に直す。"""
    midi = build_xf_midi(
        tmp_path / "particle.mid",
        notes=[(0, 240, 60), (240, 240, 62), (480, 240, 64), (720, 240, 65)],
        lyric_events=[(0, "<僕[ぼ]"), (240, "く"), (480, "は"), (720, "花[はな]")],
    )
    project = analyze_midi(midi)
    kanas = [n.kana for n in project.notes]
    assert kanas == ["ボ", "ク", "ワ", "ハナ"]  # 助詞だけ ワ、「花」の ハ は不変
    assert project.lines[0].xf_kana == "ボクワハナ"
    assert [n.surface for n in project.notes] == ["僕", "く", "は", "花"]  # 表記は不変
    assert project.notes[2].raw == "は"  # 生テキストも不変


def test_analyze_midi_keeps_non_particle_ha(tmp_path: Path):
    """助詞でない「は」は触らない。"""
    midi = build_xf_midi(
        tmp_path / "hana.mid",
        notes=[(0, 240, 60), (240, 240, 62)],
        lyric_events=[(0, "<は"), (240, "な")],
    )
    project = analyze_midi(midi)
    assert [n.kana for n in project.notes] == ["ハ", "ナ"]
