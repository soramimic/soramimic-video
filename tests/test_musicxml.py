"""Validate common synthesis/download scores against the complete official schema."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from lxml import etree

from soramimic_video.musicxml import build_musicxml
from soramimic_video.project import Note, Project, SongInfo
from soramimic_video.synthesize import build_lyric_map
from soramimic_video.xfparse import analyze_midi


@pytest.fixture(scope="module")
def schema():
    parser = etree.XMLParser(no_network=True, resolve_entities=False)
    return etree.XMLSchema(etree.parse(str(
        Path(__file__).parent / "fixtures/musicxml31/musicxml.xsd"
    ), parser))


def validate(schema, xml):
    parser = etree.XMLParser(no_network=True, resolve_entities=False)
    schema.assertValid(etree.fromstring(xml.encode(), parser))


def score(start=0, end=960):
    return Project(
        song=SongInfo("", 480, tempo_map=[[0, 500_000]]),
        notes=[Note(0, 60, start, end, start / 960, end / 960, 0, "ラ", "ラ", "ラ")],
    )


@pytest.mark.parametrize("start,end", [(0, 960), (480, 2400), (0, 1)])
def test_standard_score_preserves_lyrics_with_head_rest_and_ties(schema, start, end):
    project = score(start, end)
    xml = build_musicxml(project, build_lyric_map(project))
    validate(schema, xml)
    root = ET.fromstring(xml)
    assert [e.text for e in root.findall(".//lyric/text")] == ["ラ"]
    divisions = int(root.findtext(".//divisions"))
    notes = root.findall(".//note")
    assert notes[0].find("rest") is not None
    # The original note end is unchanged, including the one-tick note.
    cursor = 0
    pitched_end = None
    for note in notes:
        cursor += int(note.findtext("duration"))
        if note.find("pitch") is not None:
            pitched_end = cursor
    assert pitched_end / divisions == end / 480


def test_tempi_keep_their_positions_inside_notes_rests_and_measures(schema):
    project = score(480, 3000)
    project.song.tempo_map = [[0, 500_000], [240, 600_000], [720, 400_000], [1920, 750_000]]
    xml = build_musicxml(project, build_lyric_map(project))
    validate(schema, xml)
    root = ET.fromstring(xml)
    tick = 0
    tempi = []
    for measure in root.findall("./part/measure"):
        for element in measure:
            if element.tag == "note":
                tick += int(element.findtext("duration"))
            elif element.tag == "direction":
                bpm = float(element.find("sound").get("tempo"))
                assert element.findtext("direction-type/metronome/beat-unit") == "quarter"
                assert float(element.findtext("direction-type/metronome/per-minute")) == bpm
                tempi.append((tick, round(60_000_000 / bpm)))
    assert tempi == [tuple(change) for change in project.song.tempo_map]
    # Splitting a held note at tempo changes must not repeat its lyric.
    assert [e.text for e in root.findall(".//lyric/text")] == ["ラ"]
    tied = root.findall(".//note[tie]")
    assert tied[0].find("tie").get("type") == "start"
    assert tied[-1].find("tie").get("type") == "stop"


def test_mid_measure_time_signature_starts_new_measure(schema):
    project = score(480, 3000)
    project.song.time_signatures = [[0, 4, 4], [960, 3, 4]]
    xml = build_musicxml(project, build_lyric_map(project))
    validate(schema, xml)
    root = ET.fromstring(xml)
    measures = root.findall("./part/measure")
    assert sum(int(n.findtext("duration")) for n in measures[0].findall("note")) == 960
    assert measures[0].get("implicit") == "yes"
    assert measures[1].findtext("attributes/time/beats") == "3"


SAMPLES = sorted((Path(__file__).parents[1] / "src/soramimic_video/static/sample").glob("*.mid"))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_bundled_song_scores_are_valid(schema, path):
    project = analyze_midi(path)
    validate(schema, build_musicxml(project, build_lyric_map(project)))


def test_overlapping_notes_do_not_shorten_the_score_or_leave_zero_durations(schema):
    project = score(0, 960)
    project.notes.extend([
        Note(1, 62, 480, 1440, .5, 1.5, 0, "ミ", "ミ", "ミ"),
        Note(2, 64, 480, 1920, .5, 2, 0, "ソ", "ソ", "ソ"),
    ])
    xml = build_musicxml(project, build_lyric_map(project))
    validate(schema, xml)
    notes = ET.fromstring(xml).findall(".//note")
    durations = [int(note.findtext("duration")) for note in notes]
    assert all(duration > 0 for duration in durations)
    assert sum(durations) == 1920
    assert notes[-1].findtext("lyric/text") == "ソ"
    assert sum(durations[:-1]) == 480


def test_default_tempo_before_later_tempo_change(schema):
    project = score(0, 960)
    project.song.tempo_map = [[480, 1_000_000]]
    xml = build_musicxml(project, build_lyric_map(project))
    validate(schema, xml)
    root = ET.fromstring(xml)
    assert root.findtext(".//note/duration") == "29"
    assert [e.get("tempo") for e in root.findall(".//sound")] == ["120", "60"]
