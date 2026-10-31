"""Reading inference must respect source lyric events in both MIDI formats."""

import pytest
from fastapi.testclient import TestClient

from helpers import build_lyric_midi, build_xf_midi
from soramimic_video import api, reading
from soramimic_video.xfparse import analyze_midi, parse_lyric_events


@pytest.fixture(params=[build_lyric_midi, build_xf_midi], ids=["standard", "xf"])
def midi_builder(request, tmp_path):
    def build(texts):
        return request.param(
            tmp_path / "lyrics.mid",
            notes=[(i * 480, 480, 60 + i) for i in range(len(texts))],
            lyric_events=[(i * 480, text) for i, text in enumerate(texts)],
        )
    return build


def test_latin_event_readings_do_not_bleed_into_other_notes(midi_builder, monkeypatch):
    calls = []
    pronunciations = {"red": "レッド", "be": "ビー", "beautiful": "ビューティフル", "sky": "スカイ"}

    def read(text):
        calls.append(text)
        return pronunciations[text]

    monkeypatch.setattr(reading, "text_to_kana", read)
    path = midi_builder(["red", "be", "beautiful", "sky"])
    project = analyze_midi(path)
    assert calls == ["red", "be", "beautiful", "sky"]
    assert [n.kana for n in project.notes] == ["レッド", "ビー", "ビューティフル", "スカイ"]
    assert [n.start_tick for n in project.notes] == [0, 480, 960, 1440]
    assert [n.surface for n in project.notes] == ["red", "be", "beautiful", "sky"]


def test_word_readings_never_cross_japanese_event_boundaries(midi_builder, monkeypatch):
    monkeypatch.setattr(reading, "reading_tokens", lambda _: [("春", "ハル"), ("祭", "マツリ")])
    monkeypatch.setattr(reading, "text_to_kana", {"春": "ハル", "祭": "マツリ"}.__getitem__)
    project = analyze_midi(midi_builder(["春", "祭"]))
    assert [n.kana for n in project.notes] == ["ハル", "マツリ"]


def test_kanji_and_kana_in_one_event_are_read_in_full(midi_builder, monkeypatch):
    monkeypatch.setattr(reading, "reading_tokens", lambda _: [("僕", "ボク"), ("の", "ノ")])
    monkeypatch.setattr(reading, "text_to_kana", lambda _: "ボクノ")
    project = analyze_midi(midi_builder(["僕の"]))
    assert project.notes[0].kana == "ボクノ"
    assert project.notes[0].surface == "僕の"


def test_fullwidth_latin_and_digits_do_not_leave_partial_kana(midi_builder, monkeypatch):
    pronunciations = {"ＢＥ": "ビー", "さ２": "サニ"}
    monkeypatch.setattr(reading, "text_to_kana", pronunciations.__getitem__)
    project = analyze_midi(midi_builder(["ＢＥ", "さ２"]))
    assert [n.kana for n in project.notes] == ["ビー", "サニ"]
    assert [n.surface for n in project.notes] == ["ＢＥ", "さ２"]


def test_contextual_word_reading_uses_existing_kana_anchors(midi_builder, monkeypatch):
    monkeypatch.setattr(
        reading, "reading_tokens", lambda _: [("女々しく", "メメシク"), ("て", "テ")]
    )
    monkeypatch.setattr(reading, "text_to_kana", lambda text: {"女": "オンナ", "々": ""}[text])
    project = analyze_midi(midi_builder(["女", "々", "し", "く", "て"]))
    assert [n.kana for n in project.notes] == ["メ", "メ", "シ", "ク", "テ"]


def test_punctuation_preserves_contextual_reading_inference(midi_builder, monkeypatch):
    monkeypatch.setattr(
        reading, "reading_tokens", lambda _: [("「", ""), ("女々しく", "メメシク"), ("」", "")]
    )
    monkeypatch.setattr(reading, "text_to_kana", lambda text: {"「女": "オンナ", "々": ""}[text])
    project = analyze_midi(midi_builder(["「", "女", "々", "し", "く", "」"]))
    assert [n.kana for n in project.notes] == ["メ", "メ", "シ", "ク"]
    assert project.lines[0].xf_surface == "「女々しく」"


def test_contextual_long_vowels_keep_supplied_kana(midi_builder, monkeypatch):
    monkeypatch.setattr(reading, "reading_tokens", lambda _: [("大きい", "オーキー")])
    monkeypatch.setattr(reading, "text_to_kana", lambda _: "ダイ")
    project = analyze_midi(midi_builder(["大", "き", "い"]))
    assert [n.kana for n in project.notes] == ["オー", "キ", "イ"]


def test_ambiguous_split_word_requires_a_reading_instead_of_even_distribution(
    midi_builder, monkeypatch,
):
    monkeypatch.setattr(reading, "reading_tokens", lambda _: [("明日", "アシタ")])
    monkeypatch.setattr(reading, "text_to_kana", {"明": "アキラ", "日": "ヒ"}.__getitem__)
    with pytest.raises(ValueError, match=r"明.*tick=0.*表記\[かな\]"):
        analyze_midi(midi_builder(["明", "日"]))


def test_split_word_can_use_matching_local_readings(midi_builder, monkeypatch):
    monkeypatch.setattr(reading, "reading_tokens", lambda _: [("春風", "ハルカゼ")])
    monkeypatch.setattr(reading, "text_to_kana", {"春": "ハル", "風": "カゼ"}.__getitem__)
    project = analyze_midi(midi_builder(["春", "風"]))
    assert [n.kana for n in project.notes] == ["ハル", "カゼ"]


def test_supplied_readings_skip_inference(midi_builder, monkeypatch):
    def unexpected(_):
        pytest.fail("fully supplied kana must not be inferred again")

    monkeypatch.setattr(reading, "text_to_kana", unexpected)
    monkeypatch.setattr(reading, "reading_tokens", unexpected)
    project = analyze_midi(midi_builder(["青[あ]", "い", "空[そら]", "be[びー]"]))
    assert [n.kana for n in project.notes] == ["ア", "イ", "ソラ", "ビー"]


def test_english_inference_does_not_change_a_supplied_reading(midi_builder, monkeypatch):
    monkeypatch.setattr(reading, "text_to_kana", lambda _: "ビー")
    project = analyze_midi(midi_builder(["be", "sky[そら]"]))
    assert [n.kana for n in project.notes] == ["ビー", "ソラ"]


def test_multiple_lyric_events_on_one_note_keep_their_reading_text(tmp_path, monkeypatch):
    path = build_lyric_midi(tmp_path / "merged.mid", [(0, 480, 60)], [(0, "hel"), (240, "lo")])
    monkeypatch.setattr(reading, "text_to_kana", {"hello": "ハロー"}.__getitem__)
    project = analyze_midi(path)
    assert len(project.notes) == 1
    assert (project.notes[0].surface, project.notes[0].kana) == ("hello", "ハロー")


def test_ruby_closing_event_keeps_its_following_lyric():
    events = parse_lyric_events([(0, "沈[し"), (480, "ず"), (960, "]む")])
    assert [(e.surface, e.kana) for e in events] == [("沈", "し"), ("", "ず"), ("む", "む")]
    event = parse_lyric_events([(0, "沈[しず]む")])[0]
    assert (event.surface, event.kana) == ("沈む", "しずむ")


def test_display_spaces_do_not_create_empty_sung_notes(midi_builder):
    project = analyze_midi(midi_builder(["あ", " ", "い"]))
    assert [(n.start_tick, n.kana) for n in project.notes] == [(0, "ア"), (960, "イ")]
    assert project.lines[0].xf_kana == "アイ"


def test_display_punctuation_keeps_text_without_consuming_notes(midi_builder):
    project = analyze_midi(midi_builder(["あ", "「", "い", "う", "」", "。", "/（", "え", "）"]))

    assert [(n.start_tick, n.kana) for n in project.notes] == [
        (0, "ア"), (960, "イ"), (1440, "ウ"), (3360, "エ"),
    ]
    assert [line.xf_surface for line in project.lines] == ["あ「いう」。", "（え）"]
    assert [line.xf_kana for line in project.lines] == ["アイウ", "エ"]


@pytest.mark.parametrize("shared_tick", [False, True])
def test_standalone_punctuation_does_not_take_a_sung_note(tmp_path, shared_tick):
    start = 69280 if shared_tick else 69520
    path = build_xf_midi(
        tmp_path / "quoted.mid",
        notes=([(69280, 240, 59)] if not shared_tick else []) + [
            (start, 240, 60), (start + 240, 240, 62),
        ],
        lyric_events=[(69280, "「"), (start, "あ"), (start + 240, "い"), (start + 480, "」")],
    )
    project = analyze_midi(path)
    assert [(n.start_tick, n.kana) for n in project.notes] == [(start, "ア"), (start + 240, "イ")]
    assert project.lines[0].xf_surface == "「あい」"

    with TestClient(api.create_app(jobs_dir=tmp_path / "jobs")) as client:
        check = client.post(
            "/api/midi-check", files={"midi": (path.name, path.read_bytes(), "audio/midi")},
        )
    assert check.status_code == 200
    assert check.json()["has_lyrics"] is True
    assert check.json()["midi_lines"] == ["「あい」"]


def test_display_bracket_closure_keeps_ruby_state():
    events = parse_lyric_events([(0, "/「"), (240, "青[あ"), (480, "お"), (720, "]」")])
    assert [(e.tick, e.surface, e.kana, e.line_break_before) for e in events] == [
        (240, "「青", "「あ", True), (480, "」", "お」", False),
    ]


def test_punctuation_without_sung_text_is_not_a_lyric(midi_builder):
    with pytest.raises(ValueError, match="歌詞イベントがありません"):
        analyze_midi(midi_builder(["「", "」"]))


def test_empty_inferred_reading_is_rejected_before_editor_or_generation(
    midi_builder, tmp_path, monkeypatch,
):
    monkeypatch.setattr(reading, "text_to_kana", lambda _: "")
    path = midi_builder(["unreadable"])
    with TestClient(api.create_app(jobs_dir=tmp_path / "jobs")) as client:
        files = {"midi": (path.name, path.read_bytes(), "audio/midi")}
        check = client.post("/api/midi-check", files=files)
        assert check.status_code == 200
        assert check.json()["has_lyrics"] is False
        assert "表記[かな]" in check.json()["detail"]
        editor = client.post("/api/editor-session", files=files, data={"convert": "false"})
        assert editor.status_code == 400
        assert "表記[かな]" in editor.json()["detail"]


def test_real_reading_engine_keeps_an_english_word_on_its_own_note(midi_builder):
    project = analyze_midi(midi_builder(["blue", "be", "beautiful"]))
    assert project.notes[1].kana == "ビー"
    assert all(n.kana for n in project.notes)
