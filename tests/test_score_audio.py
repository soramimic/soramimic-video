import copy
import json
from types import SimpleNamespace

import pytest

from soramimic_video import score_audio


def test_score_audio_imports_supported_slots_and_records_unresolved_units(monkeypatch, tmp_path):
    layers = {
        "schema_version": 1, "canonical_text": "かき", "canonical": [
            {"utterance_id": "u0", "text": "かき", "kana": "カキ",
             "mora_ids": ["m0", "m1"]}],
        "performed": [
            {"singing_unit_id": f"s{i}", "mora_ids": [f"m{i}"],
             "start_sec": i * .3, "end_sec": (i + 1) * .3} for i in range(2)],
        "synthesis_plan": [
            {"utterance_id": "u0", "singing_unit_id": "s0", "mora_ids": ["m0"],
             "kana": "カ", "start_sec": 0., "end_sec": .3, "midi_pitch": 60,
             "pitch_sources": ["sheetsage2-vocal"]}],
        "omissions": [], "unresolved_unit_ids": ["s1"], "diagnostics": [], "evidence": [],
    }
    observed = {}

    def fake_analyze(path, **kwargs):
        observed.update(kwargs)
        assert path == tmp_path / "input.wav"
        kwargs["on_progress"]("歌詞を認識しています")
        return SimpleNamespace(
            to_dict=lambda: {"score": copy.deepcopy(layers)},
            to_json=lambda: json.dumps({"score": layers}),
        )

    import soramimic_score

    monkeypatch.setattr(soramimic_score, "analyze_audio", fake_analyze)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    source = tmp_path / "input.wav"
    source.write_bytes(b"fake")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("かき\n", encoding="utf-8")
    values = []
    project = score_audio.analyze_audio(source, tmp_path / "project", lyrics_path=lyrics,
                                        progress=values.append)

    assert observed["lyrics"] == ["かき"]
    assert observed["accompaniment_path"] == tmp_path / "project/analyze_audio/no_vocals.wav"
    assert [note.kana for note in project.notes] == ["カ"]
    assert project.lines[0].canonical_kana == "カキ"
    assert project.lyric_layers["unresolved_unit_ids"] == []
    assert len(project.lyric_layers["omissions"]) == 1
    assert values[0] == .01 and values[-1] == 1.
    analysis = json.loads((tmp_path / "project/analyze_audio/analysis.json").read_text())
    assert analysis["audio_pipeline"] == "soramimic-score"
    assert analysis["diagnostics"][0]["unit_count"] == 1
    assert json.loads((tmp_path / "project/analyze_audio/score.json").read_text())[
        "score"]["unresolved_unit_ids"] == ["s1"]


def test_score_audio_requires_positive_bpm_and_lyrics_for_adjustment(tmp_path):
    with pytest.raises(ValueError, match="入力歌詞"):
        score_audio.analyze_audio(tmp_path / "audio.wav", tmp_path, adjust_lyrics=True)
    with pytest.raises(ValueError, match="BPM"):
        score_audio.analyze_audio(tmp_path / "audio.wav", tmp_path, bpm=0)
