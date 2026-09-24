import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from soramimic_video import known_lyrics, vocal_activity
from soramimic_video.audio_melody import MelodyNote
from soramimic_video.project import Line, Note, Project, SongInfo
from soramimic_video.transcribe import TranscribedLine


def test_known_lyrics_adjustment_drops_inactive_recognition(monkeypatch, tmp_path):
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: text)
    monkeypatch.setattr(vocal_activity, "measure_vocal_activity", lambda *_: SimpleNamespace(
        lines=[SimpleNamespace(supported=True), SimpleNamespace(supported=False)],
    ))
    lines, audit = known_lyrics.adjust_supplied_lines(
        ["かき", "さし"],
        [TranscribedLine(0, 1, "かき"), TranscribedLine(1, 2, "さし")],
        [MelodyNote(0, 2, 60)], vocals=tmp_path / "vocals.wav",
    )
    assert lines == ["かき"]
    assert audit["supplied_lines"] == ["かき", "さし"]


def test_known_lyrics_adjustment_reports_unrelated_audio(monkeypatch):
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: text)
    with pytest.raises(RuntimeError, match="削除・補完をオフ"):
        known_lyrics.adjust_supplied_lines(
            ["かき"], [TranscribedLine(0, 1, "さし")], [MelodyNote(0, 1, 60)], vocals=None,
        )


def test_surface_overlay_preserves_notes_readings_and_unmatched_input(monkeypatch, tmp_path):
    readings = {"はしを渡る": "ハシオワタル", "橋を渡る": "ハシオワタル",
                "白い雲": "シロイクモ", "橋を渡る白い雲": "ハシオワタルシロイクモ",
                "遠い星": "トオイホシ"}
    monkeypatch.setattr(known_lyrics, "text_to_kana", readings.__getitem__)
    project = Project(SongInfo("", 480), [
        Note(0, 60, 0, 480, 0, 1, 0, "", "ハ", "ハ"),
        Note(1, 62, 480, 960, 1, 2, 1, "", "シ", "シ"),
    ], [Line(0, "はしを渡る", "ハ", [0], canonical_kana="ハシオワタル"),
        Line(1, "白い雲", "シ", [1], canonical_kana="シロイクモ")],
        lyric_layers={"canonical_text": "はしを渡る\n白い雲"})
    notes = copy.deepcopy(project.notes)
    source = copy.deepcopy(project.lyric_layers)
    overlay = known_lyrics.apply_supplied_surface(project, ["橋を渡る白い雲", "遠い星"])
    assert overlay["groups"][0]["asr_indices"] == [0, 1]
    assert overlay["unused_supplied_indices"] == [1]
    assert overlay["supplied_lines"] == ["橋を渡る白い雲", "遠い星"]
    assert all(line.original_text == "橋を渡る白い雲" for line in project.lines)
    assert project.lines[0].original_line_index == project.lines[1].original_line_index
    assert project.notes == notes
    assert project.lyric_layers["canonical_text"] == source["canonical_text"]
    project.save(tmp_path)
    assert asdict(Project.load(tmp_path)) == asdict(project)


@pytest.mark.parametrize("evidence,text,asr,candidates,expected", [
    ("アシタ", "明日", "明日", ["アシタ", "アス"], "アシタ"),
    ("アス", "明日", "明日", ["アシタ", "アス"], "アス"),
    ("", "明日", "明日", ["アシタ", "アス"], "アシタ"),
    ("conflict", "明日", "明日", ["アシタ", "アス"], "アシタ"),
    ("タツマチ", "たちまち", "たつまち", ["タチマチ"], "タチマチ"),
    ("アス", "｜明日《あした》", "明日", ["アシタ"], "アシタ"),
])
@pytest.mark.parametrize("fail_final", [False, True])
def test_supplied_candidates_are_fixed_before_final_alignment(
    monkeypatch, tmp_path, evidence, text, asr, candidates, expected, fail_final,
):
    import sys

    from soramimic_video import (
        analyze_audio,
        audio_melody,
        kana_whisper,
        mora_align,
        reading,
        separation,
        transcribe,
    )

    automatic = "タツマチ" if asr == "たつまち" else "アス"
    monkeypatch.setattr(reading, "automatic_reading_candidates", lambda _: [automatic])
    monkeypatch.setattr(reading, "reading_candidates", lambda _: candidates)
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda _: "アシタ")
    monkeypatch.setattr(
        analyze_audio, "_has_kana_choice", lambda vv, **_k: any(len(v) > 1 for v in vv),
    )
    monkeypatch.setattr(transcribe, "transcribe_lines", lambda *_a, **_k: [
        TranscribedLine(0, 1, asr)])
    monkeypatch.setattr(audio_melody, "configured_capabilities", lambda: {"sheetsage2": True})
    monkeypatch.setattr(audio_melody, "transcribe_sheetsage", lambda *_a, **_k: [
        MelodyNote(i * .2, (i + 1) * .2, 60 + i) for i in range(4)])
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        info=lambda _: SimpleNamespace(duration=1)))
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *_a: object())
    monkeypatch.setattr(separation, "separate", lambda *_a: (
        tmp_path / "vocals.wav", tmp_path / "bgm.wav"))
    monkeypatch.setattr(vocal_activity, "measure_vocal_activity", lambda *_a: SimpleNamespace(
        reference_dbfs=-10, lines=[vocal_activity.VocalActivityLine(-10, 0, 1, True)]))
    calls = []
    def align(_path, variants, **kwargs):
        calls.append((variants, kwargs["line_windows"]))
        if fail_final and len(calls) == 2:
            raise ValueError("insufficient frames")
        selected = variants[0][0]
        return ([mora_align.AlignedMora(0, i, k, i * .2, (i + 1) * .2, .9)
                 for i, k in enumerate(selected)], [0])
    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    def kana(path, *_a):
        return [("アス" if path.name == "vocals.wav" else "アシタ")
                if evidence == "conflict" else evidence]
    monkeypatch.setattr(kana_whisper, "transcribe_kana_windows", kana)
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text(text + "\n遠い星", encoding="utf-8")
    # Give the unrelated extra line a distinct reading so it remains unresolved.
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: (
        "トオイホシ" if text == "遠い星" else "アシタ"))
    if fail_final:
        with pytest.raises(RuntimeError, match="自動認識の読みには戻していません"):
            analyze_audio.analyze_audio(tmp_path / "input.wav", tmp_path / "project",
                                       lyrics_path=lyrics, device="cpu")
        assert len(calls) == 2
        record = json.loads((tmp_path / "project/analyze_audio/lyric_surface.json").read_text())
        assert record["alignment_status"] == "failed"
        assert record["reading_reviews"][0]["proposed"] == expected
        return
    project = analyze_audio.analyze_audio(tmp_path / "input.wav", tmp_path / "project",
                                         lyrics_path=lyrics, device="cpu")
    overlay = project.lyric_layers["lyric_surface"]
    review = overlay["reading_reviews"][0]
    assert project.lines[0].canonical_kana == expected
    assert review["status"] == "supplied-reading"
    assert review["candidates"] == candidates
    assert overlay["readings_fixed_before_alignment"] is True
    assert overlay["unused_supplied_indices"] == [1]
    assert len(calls) == 2  # provisional localization, then fixed final pronunciation
    assert "".join(calls[-1][0][0][0]) == expected
    assert project.lines[0].xf_surface == known_lyrics.strip_ruby(text)
    assert all(window == [(0, 1)] for _, window in calls)


def test_split_and_merged_lines_prepare_authoritative_text(monkeypatch):
    monkeypatch.setattr(known_lyrics, "text_to_kana", lambda text: text)
    lines, texts, plan = known_lyrics.prepare_supplied_inputs(
        [TranscribedLine(0, 1, "あおいそら"), TranscribedLine(1, 2, "しろいくも"),
         TranscribedLine(3, 4, "ねこ")],
        ["アオイソラ", "シロイクモ", "ネコ"], ["あおいそらしろいくも", "ほし"],
    )
    assert lines == [TranscribedLine(0, 2, "あおいそらしろいくも"), TranscribedLine(3, 4, "ねこ")]
    assert texts == ["あおいそらしろいくも", "ねこ"]
    assert plan["groups"][0]["asr_indices"] == [0, 1]
    assert plan["groups"][1]["line_indices"] == [1]
    assert plan["unused_supplied_indices"] == [1]
    lines, texts, plan = known_lyrics.prepare_supplied_inputs(
        lines[:1], ["アオイソラシロイクモ"], ["あおいそら", "しろいくも"],
    )
    assert texts == ["あおいそら\nしろいくも"]
    assert plan["groups"][0]["supplied_indices"] == [0, 1]
