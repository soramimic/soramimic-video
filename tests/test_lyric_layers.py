import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from soramimic_video.convert import engine_phrases
from soramimic_video.lyric_layers import apply_lyric_layers
from soramimic_video.mora_align import CTCEmissions, collapse_kana_aliases, decode_kana_window
from soramimic_video.project import Parody, ParodyLine, ParodyWord, Project, SongInfo
from soramimic_video.synthesize import build_lyric_map
from soramimic_video.voicevox import build_score
from soramimic_video.xfexport import export_xf_midi
from soramimic_video.xfparse import analyze_midi


def layers():
    return {
        "schema_version": 1, "canonical_text": "かきく",
        "canonical": [{"utterance_id": "u0", "text": "かきく", "kana": "カキク",
                       "mora_ids": ["m0", "m1", "m2"]}],
        "performed": [
            {"singing_unit_id": f"s{i}", "mora_ids": [f"m{i}"], "status": "observed",
             "start_sec": i * 0.3, "end_sec": (i + 1) * 0.3,
             "confidence": 0.7, "link_ids": [f"l{i}"], "evidence_ids": []}
            for i in range(3)
        ],
        "synthesis_plan": [
            {"id": f"slot-{i}", "utterance_id": "u0", "singing_unit_id": f"s{i}",
             "mora_ids": [f"m{i}"], "note_candidate_id": "n0", "link_ids": [f"l{i}"],
             "kana": kana, "start_sec": i * 0.3, "end_sec": (i + 1) * 0.3,
             "midi_pitch": 60, "operation": "stack_split", "timing_source": "aligned_boundary",
             "confidence": 0.7, "evidence_ids": [], "pitch_sources": ["synthetic"],
             "continuation": False}
            for i, kana in enumerate("カキク")
        ],
        "omissions": [], "unresolved_unit_ids": [], "diagnostics": [], "evidence": [],
    }


def project():
    return Project(SongInfo("", 480, tempo_map=[[0, 500000]]))


def test_layer_import_roundtrip_keeps_canonical_and_split_timing(tmp_path):
    value, data = project(), layers()
    apply_lyric_layers(value, data)
    assert engine_phrases(value) == ["カキク"]
    assert [n.kana for n in value.notes] == list("カキク")
    assert [n.start_sec for n in value.notes] == [0, 0.3, 0.6]
    assert value.notes[-1].end_sec == pytest.approx(0.9)
    value.save(tmp_path)
    assert Project.load(tmp_path) == value
    data["canonical"][0]["text"] = "changed"
    assert value.lyric_layers["canonical"][0]["text"] == "かきく"


def test_omitted_performance_does_not_remove_canonical_subtitles_or_parody_input():
    data = layers()
    data["synthesis_plan"].pop(1)
    data["omissions"] = [{"singing_unit_id": "s1", "reason": "synthetic review",
                          "evidence_ids": ["omission"]}]
    data["evidence"] = [{"id": "omission", "source": "synthetic-review",
                         "kind": "performance-omission", "confidence": 1.0, "detail": {}}]
    value = project()
    apply_lyric_layers(value, data)
    assert value.lines[0].original_text == "かきく"
    assert value.lines[0].xf_kana == "カク"
    assert engine_phrases(value) == ["カキク"]
    assert value.line_time_range(value.lines[0]) == pytest.approx((0, 0.9))


def test_wholly_omitted_line_retains_text_and_observed_subtitle_window():
    data = layers()
    data["synthesis_plan"] = []
    data["omissions"] = [{"singing_unit_id": f"s{i}", "reason": "synthetic review",
                          "evidence_ids": ["omission"]} for i in range(3)]
    data["evidence"] = [{"id": "omission", "kind": "performance-omission", "confidence": 1}]
    value = project()
    apply_lyric_layers(value, data)
    assert value.lines[0].original_text == "かきく"
    assert value.line_time_range(value.lines[0]) == pytest.approx((0, 0.9))


@pytest.mark.parametrize("failure", [
    "unresolved", "silent_deletion", "fake_omission", "overlap", "missing_membership",
])
def test_bad_plan_is_rejected_without_mutating_project(failure):
    value, data = project(), layers()
    before = copy.deepcopy(value)
    if failure == "unresolved":
        data["unresolved_unit_ids"] = ["s1"]
    elif failure == "silent_deletion":
        data["synthesis_plan"].pop()
    elif failure == "fake_omission":
        data["synthesis_plan"].pop()
        data["omissions"] = [{"singing_unit_id": "s2", "reason": "no match", "evidence_ids": []}]
    elif failure == "missing_membership":
        data["synthesis_plan"].pop(1)
        data["performed"].pop(1)
    else:
        data["synthesis_plan"][1]["start_sec"] = 0.1
    with pytest.raises(ValueError):
        apply_lyric_layers(value, data)
    assert value == before


def test_legacy_project_without_layers_still_loads(tmp_path):
    value = project()
    value.save(tmp_path)
    data = json.loads((tmp_path / "project.json").read_text())
    data.pop("lyric_layers")
    (tmp_path / "project.json").write_text(json.dumps(data))
    assert Project.load(tmp_path).lyric_layers is None


def test_parody_fallback_stacks_extra_moras_without_truncation():
    value = project()
    apply_lyric_layers(value, layers())
    value.parody = Parody("synthetic", lines=[ParodyLine(0, [ParodyWord(
        surface="かきくけこ", kana="カキクケコ", original="", original_surface="かきく",
        originalkana="カキク", note_ids=[0, 1, 2],
    )])])
    assert build_lyric_map(value) == {0: "カ", 1: "キ", 2: "クケコ"}


def test_xf_roundtrip_and_hash_bound_provenance(tmp_path: Path):
    value = project()
    apply_lyric_layers(value, layers())
    path = export_xf_midi(value, tmp_path / "selected.mid")
    restored = analyze_midi(path)
    assert [n.midi_note for n in restored.notes] == [60, 60, 60]
    assert [n.kana for n in restored.notes] == list("カキク")
    sidecar = json.loads(path.with_suffix(".mid.provenance.json").read_text())
    assert sidecar["midi_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sidecar["lyric_layers"]["canonical_text"] == "かきく"
    assert [n["kana"] for n in sidecar["selected_notes"]] == list("カキク")


def test_ctc_window_is_independent_greedy_decode_with_repeat_blanks():
    # 0.5 s padding + six 20 ms frames. Adjacent repeats collapse; blank-separated
    # repeats survive. The expected score comes from the matrix, not a constant.
    matrix = np.full((40, 3), -20.0)
    matrix[:, 0] = 0.0
    for i, token in enumerate([1, 1, 0, 1, 0, 2], 25):
        matrix[i] = -20
        matrix[i, token] = np.log(0.75)
    emissions = CTCEmissions(matrix, {"<pad>": 0, "カ": 1, "キ": 2})
    kana, score = decode_kana_window(emissions, 0, 0.12)
    assert kana == "カカキ"
    assert score == pytest.approx(0.75)


def test_layered_voicevox_keeps_stacked_target_when_next_note_is_close():
    value = project()
    apply_lyric_layers(value, layers())
    value.notes[1].kana = "キケ"
    score = build_score(value)
    assert [n["lyric"] for n in score["notes"] if n["key"] is not None] == list("カキケク")


def test_layered_voicevox_reports_unrenderable_mora_instead_of_dropping_it():
    value = project()
    apply_lyric_layers(value, layers())
    value.notes[1].end_sec = value.notes[1].start_sec + 0.001
    with pytest.raises(ValueError, match="歌詞は保持"):
        build_score(value)


def test_adjacent_ctc_windows_do_not_reuse_a_boundary_frame():
    matrix = np.full((40, 2), -20.0)
    matrix[:, 0] = 0.0
    matrix[30] = [-20.0, np.log(0.9)]
    emissions = CTCEmissions(matrix, {"<pad>": 0, "カ": 1})
    assert decode_kana_window(emissions, 0, .11)[0] == "カ"
    assert decode_kana_window(emissions, .11, .2)[0] == ""


def test_phonetic_ctc_alignment_sums_script_aliases_without_mutation():
    matrix = np.log(np.array([[.1, .89, .01], [.5, .2, .3]]))
    emissions = CTCEmissions(matrix, {"<pad>": 0, "か": 1, "カ": 2})
    merged = collapse_kana_aliases(emissions)
    assert np.exp(merged[:, 2]) == pytest.approx([.9, .5])
    assert np.exp(merged).sum(axis=1) == pytest.approx([1, 1])
    assert np.exp(matrix[:, 1]) == pytest.approx([.89, .2])
    assert np.isneginf(merged[:, 1]).all()
