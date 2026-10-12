import copy
import json
from collections import Counter
from dataclasses import asdict

import pytest

from helpers import build_xf_midi
from soramimic_video.completed_lyrics import FILENAME, save_result
from soramimic_video.editor_alignment import editor_analysis_lyrics
from soramimic_video.editor_io import export_editor, import_editor
from soramimic_video.lyric_layers import apply_lyric_layers
from soramimic_video.project import Project, SongInfo
from soramimic_video.synthesize import build_lyric_map
from soramimic_video.voicevox import build_score
from soramimic_video.xfparse import analyze_midi


def _project(tmp_path, phrases, layered):
    if not layered:
        events = []
        for phrase in phrases:
            for i, kana in enumerate(phrase):
                events.append((len(events) * 480, ("/" if i == 0 else "") + kana))
        path = build_xf_midi(
            tmp_path / "input.mid",
            notes=[(tick, 480, 60) for tick, _ in events], lyric_events=events,
        )
        return analyze_midi(path)
    canonical, performed, plan = [], [], []
    for line_id, phrase in enumerate(phrases):
        owner = f"u{line_id}"
        mids = []
        for kana in phrase:
            mid, sid = f"m{len(performed)}", f"s{len(performed)}"
            mids.append(mid)
            start = len(plan) * .25
            performed.append({
                "singing_unit_id": sid, "mora_ids": [mid], "status": "observed",
                "start_sec": start, "end_sec": start + .5, "confidence": 1,
                "link_ids": [], "evidence_ids": [],
            })
            # Two notes for one lyric mora: alignment must compare canonical
            # lyrics, not the rendered カーキー... note sequence.
            for part in (kana, "ー"):
                i = len(plan)
                plan.append({
                    "id": f"slot{i}", "utterance_id": owner, "singing_unit_id": sid,
                    "mora_ids": [mid], "kana": part, "start_sec": i * .25,
                    "end_sec": (i + 1) * .25, "midi_pitch": 60,
                    "pitch_sources": ["synthetic"],
                })
        canonical.append({"utterance_id": owner, "text": phrase, "kana": phrase,
                          "mora_ids": mids})
    project = Project(SongInfo("", 480, tempo_map=[[0, 500000]], audio_path="input.wav"))
    apply_lyric_layers(project, {
        "schema_version": 1, "canonical_text": "\n".join(phrases),
        "canonical": canonical, "performed": performed, "synthesis_plan": plan,
        "omissions": [], "unresolved_unit_ids": [], "evidence": [],
    })
    return project


def _payload(tmp_path, rows):
    csv = tmp_path / "words.csv"
    csv_rows = ["id,original,surface,pronunciation"]
    results, units = [], []
    for row in rows:
        row_words, row_units = [], []
        for source in row:
            i = len(csv_rows) - 1
            word = {
                "id": str(i), "surface": f"編集{i}", "original": f"元{i}",
                "kana": "サクラ", "pronunciation": ["サ", "ク", "ラ"],
                "period": [len(row_units), len(row_units) + len(source)],
                "locked": i % 2 == 0, "originalkana": source, "original_surface": source,
            }
            csv_rows.append(f"{i},元{i},編集{i},サクラ")
            row_words.append(word)
            row_units.extend({"pronunciation": kana, "surface_form": kana, "phrase": 0}
                             for kana in source)
        results.append(row_words)
        units.append(row_units)
    csv.write_text("\n".join(csv_rows), encoding="utf-8")
    return {"format": "soramimic-editor/1", "results": results, "unitsList": units,
            "phrases": ["".join(row) for row in rows], "wordlist": {"filepath": str(csv)}}


def _notes(project):
    return [{k: v for k, v in asdict(note).items() if k != "line"} for note in project.notes]


@pytest.mark.parametrize("layered", [False, True], ids=["xf", "audio"])
@pytest.mark.parametrize(("rows", "phrases"), [
    ([["カキ"], ["クケ"]], ["カキクケ"]),
    ([["カキクケ"]], ["カキ", "クケ"]),
    ([["カキ"], ["クケ"]], ["カ", "キクケ"]),  # same count, different breaks
    ([["カキ", "クケ"], ["コサ"]], ["カ", "キクケコサ"]),
    ([["カキ"], ["カキ"]], ["カ", "キカキ"]),  # identical repeated lyrics
    ([[], ["カキ"], [], ["クケ"], []], ["カキクケ"]),
])
def test_import_aligns_lyrics_across_breaks_and_keeps_edits(tmp_path, layered, rows, phrases):
    project = _project(tmp_path, phrases, layered)
    before = copy.deepcopy(project)
    payload = _payload(tmp_path, rows)
    path = tmp_path / "editor.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    import_editor(project, tmp_path)

    words = [word for line in project.parody.lines for word in line.words]
    expected = [word for row in payload["results"] for word in row]
    assert [w.surface for w in words] == [w["surface"] for w in expected]
    assert [w.locked for w in words] == [w["locked"] for w in expected]
    assert [w.kana for w in words] == ["サクラ"] * len(expected)
    assert [w.wordlist_row["id"] for w in words] == [w["id"] for w in expected]
    cursor = 0
    for word, source in zip(words, expected, strict=True):
        count = len(source["originalkana"]) * (2 if layered else 1)
        assert word.note_ids == list(range(cursor, cursor + count))
        cursor += count
    assert Counter(nid for w in words for nid in w.note_ids) == Counter(range(len(project.notes)))
    assert _notes(project) == _notes(before)
    assert project.lyric_layers == before.lyric_layers
    assert json.loads(path.read_text()) == payload
    assert build_score(project)["notes"]
    project.save(tmp_path)
    assert Project.load(tmp_path) == project

    # Portable results and reimport must use the rebased coordinates as well.
    save_result(project, tmp_path)
    assert (tmp_path / FILENAME).is_file()
    export_editor(project, tmp_path, wordlist_entry=payload["wordlist"])
    fresh = _project(tmp_path, phrases, layered)
    import_editor(fresh, tmp_path)
    assert build_lyric_map(fresh) == build_lyric_map(project)
    assert [w.surface for row in fresh.parody.lines for w in row.words] == [
        w.surface for w in words
    ]


@pytest.mark.parametrize("layered", [False, True], ids=["xf", "audio"])
@pytest.mark.parametrize(("source", "target", "counts"), [
    (["コウ", "エン"], "コーエン", [2, 2]),
    (["カキ", "クケ"], "カギクケ", [2, 2]),
    (["カキ", "クケ"], "カキアクケ", [3, 2]),
    (["カキア", "クケ"], "カキクケ", [2, 2]),
])
def test_import_aligns_reading_variants_before_assigning_notes(
    tmp_path, layered, source, target, counts,
):
    project = _project(tmp_path, [target], layered)
    payload = _payload(tmp_path, [[text] for text in source])
    (tmp_path / "editor.json").write_text(json.dumps(payload), encoding="utf-8")
    import_editor(project, tmp_path)
    words = [word for line in project.parody.lines for word in line.words]
    assert [len(w.note_ids) for w in words] == [n * (2 if layered else 1) for n in counts]
    assert [w.surface for w in words] == ["編集0", "編集1"]


@pytest.mark.parametrize(("target_lengths", "layered"), [
    ([6, 6, 6, 4], True), ([2, 4] * 3 + [2, 2], False),
])
def test_eleven_editor_lines_import_into_four_audio_or_eight_xf_lines(
    tmp_path, target_lengths, layered,
):
    text = "カキクケコサシスセソタチツテトナニヌネノハヒ"
    rows = [[text[i:i + 2]] for i in range(0, len(text), 2)]
    phrases, cursor = [], 0
    for count in target_lengths:
        phrases.append(text[cursor:cursor + count])
        cursor += count
    assert cursor == len(text)
    project = _project(tmp_path, phrases, layered)
    payload = _payload(tmp_path, rows)
    (tmp_path / "editor.json").write_text(json.dumps(payload), encoding="utf-8")
    import_editor(project, tmp_path)
    assert len(project.lines) == len(phrases)
    assert [w.surface for row in project.parody.lines for w in row.words] == [
        f"編集{i}" for i in range(11)
    ]


def test_unrelated_lyrics_do_not_discard_edits_or_mutate_project(tmp_path):
    project = _project(tmp_path, ["カキクケ"], True)
    payload = _payload(tmp_path, [["ハヒ"], ["フヘ"]])
    (tmp_path / "editor.json").write_text(json.dumps(payload), encoding="utf-8")
    before = copy.deepcopy(project)
    with pytest.raises(ValueError, match="元歌詞と曲の歌詞を対応付けられません"):
        import_editor(project, tmp_path)
    assert project == before


def test_editor_analysis_keeps_known_original_reading_without_rereading_text(tmp_path, monkeypatch):
    from soramimic_score import LyricLine
    from soramimic_score.readings import dictionary_readings

    payload = _payload(tmp_path, [["コンニチ"], [], ["ハレ"]])
    payload["phrases"] = ["今日", "", "晴れ"]
    payload["lyrics"] = "今日晴れ"  # Different display breaks must not replace known readings.
    payload["originalLines"] = ["untrusted display mapping"] * 3

    def no_reading_inference(*args, **kwargs):
        raise AssertionError("JSON already contains the original pronunciation")

    import soramimic_yomi
    monkeypatch.setattr(soramimic_yomi, "get_yomi_candidates", no_reading_inference)
    text = editor_analysis_lyrics(payload)
    assert text == "｜今日《コンニチ》\n｜晴れ《ハレ》"
    selected = dictionary_readings(None, tuple(LyricLine(line) for line in text.splitlines()))
    assert [reading.kana for reading in selected] == ["コンニチ", "ハレ"]
    assert all(reading.source == "explicit-ruby" for reading in selected)


@pytest.mark.parametrize("kind", ["midi", "audio"])
def test_pipeline_uses_json_readings_before_alignment_and_preserves_edits(
    tmp_path, monkeypatch, kind,
):
    from contextlib import contextmanager

    import soramimic_score.media
    import soramimic_score.models
    from soramimic_score import AlignedMora, AudioAdapters, MelodyNote
    from soramimic_score.japanese import kana_to_moras

    from soramimic_video import api

    payload = _payload(tmp_path, [["コンニチ"], ["ハレ"]])
    payload["phrases"] = ["今日", "晴れ"]
    payload["lyrics"] = "今日晴れ"
    (tmp_path / "editor.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "lyrics.txt").write_text(payload["lyrics"], encoding="utf-8")
    # XF supplies one line with a different reading of 今日.
    recognized = _project(tmp_path, ["キョーハレ"], False)
    notes_before = _notes(recognized)
    alignment_calls = []
    def forbidden(*_args):
        pytest.fail("JSON must use its fixed reading without recognition")

    def align(_path, lines, readings):
        alignment_calls.append([reading.kana for reading in readings])
        assert all(line.start_sec is None and line.end_sec is None for line in lines)
        result = []
        offset = 0
        for i, reading in enumerate(readings):
            moras = kana_to_moras(reading.kana)
            result.extend(AlignedMora(i, j, mora, (offset + j) * .5,
                                      (offset + j + 1) * .5, .99)
                          for j, mora in enumerate(moras))
            offset += len(moras)
        return tuple(result)

    adapters = AudioAdapters(
        reading_selector=forbidden, mora_aligner=align,
        melody_transcriber=lambda _: tuple(MelodyNote(i * .5, (i + 1) * .5, 60)
                                            for i in range(6)),
        lyric_recognizer=forbidden, lyric_reading=forbidden,
        audio_duration=lambda _: 3.,
    )

    @contextmanager
    def prepared(*_args, **_kwargs):
        yield adapters

    @contextmanager
    def decoded(path):
        yield path

    monkeypatch.setattr(soramimic_score.models, "prepared_adapters", prepared)
    monkeypatch.setattr(soramimic_score.media, "decoded_audio", decoded)
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    (tmp_path / "input.wav").write_bytes(b"deterministic model boundary")
    captured = []

    class ReachedSynthesis(Exception):
        pass

    def synthesize(_job, _config, project, *_args):
        captured.append(project)
        raise ReachedSynthesis

    monkeypatch.setattr(api, "_run_synthesize", synthesize)
    job = api.Job(id="edited", dir=tmp_path, params={
        "input_kind": kind, "auto_lyrics": False, "wordlist": str(tmp_path / "words.csv"),
    })
    with pytest.raises(ReachedSynthesis):
        api.run_pipeline(job, {"parallel_video": False})
    project, = captured
    assert "".join(line.canonical_kana or line.xf_kana for line in project.lines) == "コンニチハレ"
    words = [word for line in project.parody.lines for word in line.words]
    assert [word.surface for word in words] == ["編集0", "編集1"]
    assert [word.kana for word in words] == ["サクラ", "サクラ"]
    assert (tmp_path / "lyrics.txt").read_text() == "今日晴れ"
    assert json.loads((tmp_path / "editor.json").read_text()) == payload
    if kind == "audio":
        assert alignment_calls == [["コンニチ", "ハレ"]]
    else:
        assert _notes(project) == notes_before
