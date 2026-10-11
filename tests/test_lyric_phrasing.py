import copy
from dataclasses import asdict

import pytest

from soramimic_video.convert import (
    _layer_unit_note_indices,
    convert_project,
    engine_phrases,
    project_word_boundaries,
)
from soramimic_video.kana import split_fine_moras
from soramimic_video.lyric_layers import apply_lyric_layers
from soramimic_video.lyric_phrasing import _surface_boundaries, prepare_lyric_phrases
from soramimic_video.project import Project, SongInfo
from soramimic_video.soramimic_engine import run_tokenize
from soramimic_video.video import build_ass

TEXT = "朝の光が窓を照らして小さな鳥が空を渡って静かな街に歌が広がる"
KANA = "アサノヒカリガマドヲテラシテチーサナトリガソラヲワタッテシズカナマチニウタガヒロガル"


def _project(*, text=TEXT, kana=KANA, rest_at=(), whisper_at=(), repeat=1):
    moras = split_fine_moras(kana)
    canonical, performed, plan = [], [], []
    clock = 0.0
    recognized = []
    for i in range(repeat):
        uid = f"u{i}"
        mids = [f"{uid}-m{j}" for j in range(len(moras))]
        canonical.append({"utterance_id": uid, "text": text, "kana": kana, "mora_ids": mids})
        recognition_start = clock
        for j, (mid, mora) in enumerate(zip(mids, moras, strict=True)):
            if j in rest_at:
                clock += .6
            if j in whisper_at:
                recognized.append({"text": "認識ミスでも入力を保持", "start_sec": recognition_start,
                                   "end_sec": clock - .02})
                recognition_start = clock
            unit = f"{uid}-s{j}"
            performed.append({"singing_unit_id": unit, "mora_ids": [mid],
                              "start_sec": clock, "end_sec": clock + .25})
            plan.append({"utterance_id": uid, "singing_unit_id": unit, "mora_ids": [mid],
                         "kana": mora, "start_sec": clock, "end_sec": clock + .25,
                         "midi_pitch": 60 + j % 5, "pitch_sources": ["test"]})
            clock += .25
        recognized.append({"text": "認識した行", "start_sec": recognition_start, "end_sec": clock})
        clock += 1
    overlay = {"mode": "supplied-lyrics-first", "recognized_lines": recognized,
               "supplied_lines": [text] * repeat}
    layers = {"schema_version": 1, "canonical_text": "\n".join([text] * repeat),
              "canonical": canonical, "performed": performed, "synthesis_plan": plan,
              "omissions": [], "unresolved_unit_ids": [], "evidence": [
                  {"id": "lyric-surface", "kind": "lyric-surface", "detail": overlay}]}
    project = Project(SongInfo("", 480, tempo_map=[[0, 500000]], audio_path="test.wav"))
    apply_lyric_layers(project, layers)
    return project


def test_whisper_is_snapped_to_bunsetsu_without_requiring_a_break_at_every_rest():
    # Whisper ends inside 照らして; the safe end is mora 14. The later rest is optional.
    source = _project(whisper_at=(13,), rest_at=(28,))
    outputs = {}
    for strategy in ("whisper", "rest", "hybrid"):
        project = copy.deepcopy(source)
        prepare_lyric_phrases(project, enabled=True, strategy=strategy)
        outputs[strategy] = [line.original_text for line in project.lines]
        assert "".join(outputs[strategy]) == TEXT
        assert project.lyric_layers == source.lyric_layers
    assert outputs["whisper"] == [TEXT[:10], TEXT[10:]]
    assert outputs["rest"] == [TEXT[:20], TEXT[20:]]
    assert outputs["hybrid"] == outputs["whisper"]


def test_rests_help_locate_a_needed_split_without_creating_short_phrases():
    project = _project(rest_at=(21,))
    prepare_lyric_phrases(project, enabled=True)
    assert [line.original_text for line in project.lines] == [TEXT[:15], TEXT[15:]]
    frequent = _project(rest_at=(7, 14, 21, 28, 35))
    prepare_lyric_phrases(frequent, enabled=True)
    assert len(frequent.lines) == 2
    assert "".join(line.original_text for line in frequent.lines) == TEXT


def test_no_recognition_or_rests_still_bounds_long_phrases_without_forcing_word_cuts():
    project = _project()
    project.lyric_layers["evidence"][0]["detail"]["recognized_lines"] = []
    prepare_lyric_phrases(project, enabled=True)
    assert len(project.lines) > 1
    assert all(len(split_fine_moras(line.canonical_kana)) <= 32 for line in project.lines)
    assert "".join(line.original_text for line in project.lines) == TEXT
    single = _project(text="Supercalifragilisticexpialidocious", kana="ア" * 40)
    prepare_lyric_phrases(single, enabled=True)
    assert len(single.lines) == 1  # No trustworthy bunsetsu boundary inside this word.


def test_short_authored_lines_remain_separate_even_with_internal_hints():
    project = _project(text=TEXT[:10], kana=KANA[:14], rest_at=(7,), whisper_at=(7,), repeat=2)
    before = [line.original_text for line in project.lines]
    prepare_lyric_phrases(project, enabled=True)
    assert [line.original_text for line in project.lines] == before
    assert [line.original_line_index for line in project.lines] == [0, 1]


def test_roundtrip_repeated_conversion_and_disable_preserve_canonical_graph(tmp_path):
    project = _project(whisper_at=(14, 28), repeat=2)
    original = copy.deepcopy(project)
    prepare_lyric_phrases(project, enabled=True)
    once = copy.deepcopy(project)
    project.save(tmp_path)
    project = Project.load(tmp_path)
    prepare_lyric_phrases(project, enabled=True)
    assert project == once
    assert len(project.lines) == 6
    assert [line.original_line_index for line in project.lines] == list(range(6))
    units = run_tokenize(engine_phrases(project))
    all_notes = []
    for line, row in zip(project.lines, units, strict=True):
        groups = _layer_unit_note_indices(project, line, row)
        all_notes.extend(line.note_ids)
        assert set(i for group in groups for i in group) == set(range(len(line.note_ids)))
    assert all_notes == list(range(len(project.notes)))
    assert project_word_boundaries(project)(units) is not None
    prepare_lyric_phrases(project, enabled=False)
    assert project == original


def test_cannot_split_one_performed_unit_even_at_a_long_rest():
    project = _project(rest_at=(14,))
    layers = project.lyric_layers
    # A melisma/contracted unit owns both sides of the otherwise ideal boundary.
    left, right = layers["performed"][13:15]
    left["mora_ids"] += right["mora_ids"]
    left["end_sec"] = right["end_sec"]
    del layers["performed"][14]
    for slot in layers["synthesis_plan"][13:15]:
        slot["singing_unit_id"] = left["singing_unit_id"]
        slot["mora_ids"] = left["mora_ids"].copy()
    prepare_lyric_phrases(project, enabled=True)
    assert all(line.canonical_mora_ids[-1] != "u0-m13" for line in project.lines)
    assert sum(len(line.note_ids) for line in project.lines) == len(project.notes)


def test_unvoiced_line_after_split_keeps_its_canonical_owner():
    project = _project(whisper_at=(14, 28), repeat=2)
    layers = project.lyric_layers
    layers["synthesis_plan"] = [s for s in layers["synthesis_plan"] if s["utterance_id"] == "u0"]
    layers["evidence"].append({"id": "absent", "kind": "performance-omission", "confidence": 1.})
    layers["omissions"] = [
        {"singing_unit_id": unit["singing_unit_id"], "reason": "unvoiced",
         "evidence_ids": ["absent"]}
        for unit in layers["performed"] if unit["singing_unit_id"].startswith("u1-")
    ]
    apply_lyric_layers(project, layers)
    prepare_lyric_phrases(project, enabled=True)
    assert len(project.lines) == 4
    last = project.lines[-1]
    assert last.note_ids == [] and last.canonical_line_index == 1
    units = run_tokenize(engine_phrases(project))
    assert _layer_unit_note_indices(project, last, units[-1]) == [[] for _ in units[-1]]


def test_surface_mapping_preserves_spaces_and_avoids_ambiguous_custom_readings():
    text = "朝の光が  窓を照らして小さな鳥が空を渡って"
    boundaries = _surface_boundaries(text, KANA[:28])
    assert (6, 7) in boundaries
    assert _surface_boundaries(
        "光が 輝いて いるから空を見ていた", "ヒカリガカガヤイテイルカラソラヲミテイタ",
    ) == [(3, 4), (11, 13), (13, 16)]
    # A non-dictionary pronunciation can be kept whole, but is not divided by interpolation.
    assert _surface_boundaries("朝の光が", "アオゾラ") == []
    assert _surface_boundaries("｜朝《あさ》の光が", "アサノヒカリガ") == []


@pytest.mark.parametrize("separator", ["　", " \t　 ", "\n　"])
def test_default_phrasing_preserves_full_width_and_mixed_whitespace(separator):
    text = TEXT[:10] + separator + TEXT[10:]
    project = _project(text=text, whisper_at=(14, 28))
    layers = copy.deepcopy(project.lyric_layers)

    prepare_lyric_phrases(project)

    assert len(project.lines) == 3
    assert project.lines[0].original_text == TEXT[:10] + separator
    assert "".join(line.original_text for line in project.lines) == text
    assert "".join(line.canonical_kana for line in project.lines) == KANA
    assert [nid for line in project.lines for nid in line.note_ids] == list(
        range(len(project.notes)),
    )
    assert project.lyric_layers == layers


def test_convert_defaults_reach_engine_and_captions_and_disable_restores_input(
    monkeypatch, tmp_path,
):
    from soramimic_video import convert

    project = _project(whisper_at=(14, 28))
    observed = []
    real_run = convert.run_convert
    wordlist = tmp_path / "words.csv"
    wordlist.write_text("id,original,surface,pronunciation\n0,青空,青空,アオゾラ", encoding="utf-8")

    def run(phrases, *args, **kwargs):
        observed.append(phrases)
        return real_run(phrases, *args, **kwargs)

    monkeypatch.setattr(convert, "run_convert", run)
    original_notes = [asdict(note) for note in project.notes]
    convert_project(project, str(wordlist))
    assert len(observed[-1]) == 3
    assert [line.line_id for line in project.parody.lines] == [0, 1, 2]
    ass = build_ass(project, 1280, 720, "Font")
    captions = [line.split(",,")[-1].split("}")[-1] for line in ass.splitlines()
                if line.startswith("Dialogue:") and ",Original," in line]
    assert captions == [TEXT[:10], TEXT[10:20], TEXT[20:]]
    # Repeated conversion keeps the default phrases and their source mapping.
    first_phrases = observed[-1]
    convert_project(project, str(wordlist))
    assert observed[-1] == first_phrases
    convert_project(project, str(wordlist), auto_phrase_lyrics=False)
    assert observed[-1] == [KANA]
    assert [asdict(note) for note in project.notes] == original_notes


@pytest.mark.parametrize("has_layers", [False, True])
def test_automatic_or_midi_projects_are_untouched_by_default(has_layers):
    project = _project()
    project.lyric_layers["evidence"] = []
    if not has_layers:
        project.lyric_layers = None
    original = copy.deepcopy(project)
    prepare_lyric_phrases(project)
    assert project == original
    prepare_lyric_phrases(project, enabled=False)
    assert project == original
    with pytest.raises(ValueError, match="入力歌詞付き"):
        prepare_lyric_phrases(project, enabled=True)
    assert project == original


def test_real_score_supplied_lyrics_import_supports_conversion_slices(monkeypatch, tmp_path):
    import soramimic_score
    from soramimic_score import AlignedMora, AudioAdapters, LyricLine, MelodyNote, ReadingSelection

    from soramimic_video import score_audio

    moras = split_fine_moras(KANA)
    duration = len(moras) * .25
    adapters = AudioAdapters(
        reading_selector=lambda _path, _lines: (ReadingSelection(KANA, "fixture", 1.0),),
        lyric_reading=lambda text: {TEXT: KANA, TEXT[:10]: KANA[:14],
                                   TEXT[10:20]: KANA[14:28], TEXT[20:]: KANA[28:]}[text],
        mora_aligner=lambda _path, _lines, _readings: tuple(
            AlignedMora(0, i, m, i * .25, (i + 1) * .25, 1.) for i, m in enumerate(moras)),
        melody_transcriber=lambda _path: tuple(
            MelodyNote(i * .25, (i + 1) * .25, 60 + i % 5) for i in range(len(moras))),
        lyric_recognizer=lambda _path: (LyricLine(TEXT[:10], 0., 3.5),
                                       LyricLine(TEXT[10:20], 3.5, 7.),
                                       LyricLine(TEXT[20:], 7., duration)),
    )
    real_analyze = soramimic_score.analyze_audio
    monkeypatch.setattr(soramimic_score, "analyze_audio",
                        lambda path, **kw: real_analyze(path, adapters, lyrics=kw["lyrics"]))
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_MODEL_DIR", "/models/sheetsage")
    monkeypatch.setenv("SORAMIMIC_SHEETSAGE_BASE_DIR", "/models/mert")
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"deterministic model adapter boundary")
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text(TEXT, encoding="utf-8")
    project = score_audio.analyze_audio(audio, tmp_path / "project", lyrics_path=lyrics)
    layers = copy.deepcopy(project.lyric_layers)
    prepare_lyric_phrases(project, enabled=True)
    assert [line.original_text for line in project.lines] == [TEXT[:10], TEXT[10:20], TEXT[20:]]
    assert project.lyric_layers == layers
    for line, units in zip(project.lines, run_tokenize(engine_phrases(project)), strict=True):
        assert _layer_unit_note_indices(project, line, units)
