"""Standard lyric MIDI input through parsing, upload checking, editing, and generation."""

import threading

import mido
import pytest
from fastapi.testclient import TestClient

from helpers import build_lyric_midi, build_xf_midi
from soramimic_video import api, convert
from soramimic_video.mix import make_accompaniment_midi
from soramimic_video.xfparse import analyze_midi, melody_continuations

NOTES = [(0, 240, 60), (240, 240, 62), (960, 240, 64), (1200, 240, 65)]
LYRICS = [(0, "は"), (240, "る\r"), (960, "な"), (1200, "つ")]


@pytest.mark.parametrize(("builder", "surface"), [
    (build_xf_midi, "あ い！"),
    (build_lyric_midi, "あ^い！"),
], ids=["xf", "standard"])
def test_only_xf_decodes_caret_as_space(tmp_path, builder, surface):
    path = builder(
        tmp_path / "caret.mid",
        notes=[(0, 240, 60), (240, 240, 62)],
        lyric_events=[(0, "あ^"), (240, "い！")],
    )
    project = analyze_midi(path)
    assert project.lines[0].xf_surface == surface
    assert project.lines[0].xf_kana == "アイ"


@pytest.mark.parametrize("charset", ["cp932", "utf-8"])
@pytest.mark.parametrize("separate", [False, True])
def test_standard_lyrics_keep_text_notes_and_timing(tmp_path, charset, separate):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS,
                            charset=charset, separate_lyrics=separate)
    project = analyze_midi(path)
    assert b"XFKM" not in path.read_bytes()
    assert [line.xf_surface for line in project.lines] == ["はる", "なつ"]
    assert [line.xf_kana for line in project.lines] == ["ハル", "ナツ"]
    assert [note.midi_note for note in project.notes] == [60, 62, 64, 65]
    assert [(n.start_tick, n.end_tick) for n in project.notes] == [
        (0, 240), (240, 480), (960, 1200), (1200, 1440),
    ]
    assert project.notes[2].start_sec == 1.0
    assert project.notes[3].end_sec == 1.5


@pytest.mark.parametrize("marker", ["\r", "\n", "\r\n", "/", "\\"])
@pytest.mark.parametrize("position", ["leading", "trailing", "standalone"])
def test_standard_lyric_line_breaks(tmp_path, marker, position):
    lyrics = [(0, "は"), (240, "る"), (960, "な"), (1200, "つ")]
    if position == "leading":
        lyrics[2] = (960, marker + "な")
    elif position == "trailing":
        lyrics[1] = (240, "る" + marker)
    else:
        lyrics.insert(2, (480, marker))
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, lyrics)
    assert [line.xf_surface for line in analyze_midi(path).lines] == ["はる", "なつ"]


def test_type_zero_tempo_changes_and_multi_mora_lyrics(tmp_path):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS, midi_type=0)
    midi = mido.MidiFile(path, charset="utf-8")
    # At tick 960 change to 60 BPM. The two remaining notes keep their ticks.
    index = next(i for i, msg in enumerate(midi.tracks[0]) if msg.time == 480)
    midi.tracks[0].insert(index, mido.MetaMessage("set_tempo", tempo=1000000, time=480))
    midi.tracks[0][index + 1].time = 0
    next(msg for msg in midi.tracks[0] if msg.type == "lyrics").text = "はる"
    midi.save(path)
    project = analyze_midi(path)
    assert project.notes[0].kana == "ハル"
    assert project.notes[2].start_sec == 1.0
    assert project.notes[3].end_sec == 2.0


def test_local_vocal_track_wins_over_simultaneous_backing_and_drums(tmp_path):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS, channel=3)
    midi = mido.MidiFile(path, charset="utf-8")
    for channel in [0, 9]:
        backing = mido.MidiTrack()
        previous = 0
        for tick, duration, _ in NOTES:
            backing.append(mido.Message("note_on", channel=channel, note=48,
                                        velocity=80, time=tick - previous))
            backing.append(mido.Message("note_off", channel=channel, note=48, time=duration))
            previous = tick + duration
        midi.tracks.insert(0, backing)
    midi.save(path)
    project = analyze_midi(path)
    assert project.song.melody_channel == 3
    assert [n.midi_note for n in project.notes] == [60, 62, 64, 65]
    accompaniment = make_accompaniment_midi(project, tmp_path / "backing.mid")
    channels = {msg.channel for track in mido.MidiFile(accompaniment).tracks
                for msg in track if msg.type == "note_on"}
    assert channels == {0, 9}


def test_separate_lyric_track_honors_channel_prefix(tmp_path):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS,
                            channel=3, separate_lyrics=True)
    midi = mido.MidiFile(path, charset="utf-8")
    backing = mido.MidiTrack([
        msg.copy(channel=0) if hasattr(msg, "channel") else msg.copy()
        for msg in midi.tracks[0]
    ])
    midi.tracks.insert(0, backing)
    midi.tracks[-1].insert(0, mido.MetaMessage("channel_prefix", channel=3))
    midi.save(path)
    assert analyze_midi(path).song.melody_channel == 3


def test_multiple_vocal_parts_are_not_concatenated(tmp_path):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS)
    midi = mido.MidiFile(path, charset="utf-8")
    harmony = mido.MidiTrack([
        msg.copy(channel=4) if hasattr(msg, "channel") else
        msg.copy(text="あ") if msg.type == "lyrics" else msg.copy()
        for msg in midi.tracks[0]
    ])
    midi.tracks.append(harmony)
    midi.save(path)
    project = analyze_midi(path)
    assert len(project.notes) == 4
    assert project.lines[0].xf_surface == "はる"


def test_xf_lyrics_still_take_precedence(tmp_path):
    path = build_xf_midi(tmp_path / "song.mid", NOTES, [(0, "し"), (240, "ず")])
    # Replace only the ordinary tracks, keeping the XF chunks intact.
    content = path.read_bytes()
    xf = content[content.index(b"XFIH"):]
    build_lyric_midi(path, NOTES, LYRICS, charset="cp932")
    path.write_bytes(path.read_bytes() + xf)
    assert [n.kana for n in analyze_midi(path).notes] == ["シ", "ズ"]


def test_standard_utf8_melisma_can_read_source_again(tmp_path):
    path = build_lyric_midi(tmp_path / "song.mid",
                            [(0, 240, 60), (240, 240, 62), (480, 240, 64)],
                            [(0, "わ"), (480, "ら")])
    project = analyze_midi(path)
    assert [n.note for n in melody_continuations(project)[0]] == [62]


@pytest.mark.parametrize("lyrics", [[], [(0, "\r\n")], [(4000, "あ")]])
def test_unusable_lyrics_are_rejected(tmp_path, lyrics):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, lyrics)
    with pytest.raises(ValueError, match="歌詞"):
        analyze_midi(path)


def test_upload_check_and_editor_session_accept_standard_lyrics(tmp_path):
    path = build_lyric_midi(tmp_path / "song.midi", NOTES, LYRICS)
    client = TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))
    files = {"midi": (path.name, path.read_bytes(), "audio/midi")}
    check = client.post("/api/midi-check", files=files)
    assert check.status_code == 200, check.text
    assert check.json()["has_lyrics"] is True
    assert check.json()["midi_lines"] == ["はる", "なつ"]
    session = client.post("/api/editor-session", files=files, data={"convert": "false"})
    assert session.status_code == 200, session.text
    assert session.json()["phrases"] == ["ハル", "ナツ"]


def test_job_upload_reaches_conversion_with_standard_lyrics(tmp_path, monkeypatch):
    path = build_lyric_midi(tmp_path / "song.mid", NOTES, LYRICS)
    captured = []
    reached = threading.Event()

    def capture(project, **kwargs):
        captured.append(project)
        reached.set()
        raise RuntimeError("conversion reached")

    monkeypatch.setattr(convert, "convert_project", capture)
    with TestClient(api.create_app(jobs_dir=tmp_path / "jobs")) as client:
        response = client.post("/api/jobs", files={
            "midi": (path.name, path.read_bytes(), "audio/midi"),
        }, data={"wordlist": "stations"})
        assert response.status_code == 200, response.text
        assert reached.wait(5), "uploaded MIDI did not reach conversion"
    assert len(captured) == 1
    assert [line.xf_surface for line in captured[0].lines] == ["はる", "なつ"]
    assert captured[0].song.melody_channel == 0
