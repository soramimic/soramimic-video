"""The same wire fixtures are used by Soramimic's JavaScript consumer."""

import json
import subprocess
from pathlib import Path

import pytest

from soramimic_video.lyric_alignment import (
    align_tokens,
    sync_editor_alignments,
    valid_alignment,
)
from soramimic_video.project import Line, Note, Project, SongInfo

CASES = json.loads((Path(__file__).parent / "fixtures/lyric-alignment.json").read_text())


def test_embedded_editor_and_video_share_the_wire_contract():
    source = (Path(__file__).parents[1] / "external/soramimic/frontend/src/lyricAlignment.js")
    script = """
        import fs from 'node:fs';
        const {alignLyricTokens} = await import(process.argv[1]);
        const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
        console.log(JSON.stringify(cases.map(c => alignLyricTokens(c.text,c.reading,c.tokens))));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, source.as_uri()],
        input=json.dumps(CASES), text=True, capture_output=True, check=True, timeout=15,
    )
    assert json.loads(result.stdout) == [case["expected"] for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_wire_fixtures(case):
    text, reading = case["text"], case["reading"]
    value = align_tokens(text, reading, case["tokens"])
    assert value == case["expected"]
    assert valid_alignment(value, text, reading)
    assert not valid_alignment(value, text + "x", reading)
    assert not valid_alignment(value, text, reading + "x")


def test_old_new_and_unknown_optional_json(monkeypatch):
    import soramimic_video.lyric_alignment as module

    def build(text, reading):
        return align_tokens(text, reading, [("あ、", "ア"), ("空", "ソラ")])

    monkeypatch.setattr(module, "build_alignment", build)
    payload = {"phrases": ["あ空"], "originalLines": ["あ、空"],
               "unitsList": [[{"pronunciation": "ア"}, {"pronunciation": "ソラ"}]]}
    before = json.dumps(payload)
    sync_editor_alignments(payload)
    value = payload["originalReadingAlignments"][0]
    assert valid_alignment(value, "あ、空", "アソラ")
    unchanged = {k: v for k, v in payload.items() if k != "originalReadingAlignments"}
    assert json.dumps(unchanged) == before
    restored = json.loads(json.dumps(payload))
    sync_editor_alignments(restored)
    assert restored == payload
    restored["originalReadingAlignments"][0]["format"] = "future-version"
    sync_editor_alignments(restored)
    assert restored == payload
    restored["unitsList"][0][0]["pronunciation"] = "カ"
    assert not valid_alignment(value, "あ、空", "カソラ")


def test_subtitles_use_saved_surface_reading_spans(monkeypatch, tmp_path):
    from soramimic_video import subtitle_pages
    from soramimic_video.align import SubtitleSegment

    text, reading = "青い空", "アオイソラ"
    notes = [Note(i, 60, i * 480, (i + 1) * 480, i * .4, (i + 1) * .4,
                  0, ch, ch, ch) for i, ch in enumerate(reading)]
    project = Project(SongInfo("", 480), notes, [Line(0, reading, reading, list(range(5)), text)])
    value = align_tokens(text, reading, [("青い", "アオイ"), ("空", "ソラ")])
    project.original_reading_alignments = [value]
    project.save(tmp_path)
    restored = Project.load(tmp_path)
    assert restored.original_reading_alignments == [value]

    def unexpected(_text):
        raise AssertionError("saved correspondence must replace reading re-estimation")

    monkeypatch.setattr(subtitle_pages, "_default_reader", unexpected)
    atoms = subtitle_pages._original_atoms(
        restored, SubtitleSegment(text, 0, 2, [0]), restored.lines,
    )
    assert [a.text for a in atoms] == ["青い", "空"]
    assert atoms[1].start == pytest.approx(notes[3].start_sec)

    # Old project files have no optional maps; no schema migration is required.
    path = tmp_path / "project.json"
    old = json.loads(path.read_text())
    del old["original_reading_alignments"]
    path.write_text(json.dumps(old))
    assert Project.load(tmp_path).original_reading_alignments is None


def test_invalid_or_stale_maps_are_optional():
    value = CASES[0]["expected"]
    bad = json.loads(json.dumps(value))
    bad["spans"][0]["surface"][1] = 10000
    assert not valid_alignment(bad, value["text"], value["reading"])
    bad = json.loads(json.dumps(CASES[3]["expected"]))
    bad["spans"][0]["surface"][1] = 1
    assert not valid_alignment(bad, bad["text"], bad["reading"])
    assert not valid_alignment({"format": "future-version"}, "", "")
    assert not valid_alignment(value, value["text"], None)
