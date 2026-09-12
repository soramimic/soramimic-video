"""Export the selected vocal realization with a hash-bound provenance sidecar."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

from .project import Project
from .synthesize import build_lyric_map


def _xf_track(name: bytes, messages: list[MetaMessage], ticks_per_beat: int) -> bytes:
    midi = MidiFile(type=0, ticks_per_beat=ticks_per_beat, charset="cp932")
    midi.tracks.append(MidiTrack(messages + [MetaMessage("end_of_track")]))
    stream = io.BytesIO()
    midi.save(file=stream)
    # Standard MIDI header is MThd + uint32(6) + the six-byte header payload.
    track = stream.getvalue()[14:]
    if not track.startswith(b"MTrk"):
        raise RuntimeError("MIDI track serialization failed")
    return name + track[4:]


def export_xf_midi(project: Project, path: Path) -> Path:
    """Write current notes/kana; never export a canonical omission as a new note."""
    lyric_map = build_lyric_map(project)
    tpb = project.song.ticks_per_beat
    midi = MidiFile(type=1, ticks_per_beat=tpb, charset="cp932")
    events: list[tuple[int, int, Message | MetaMessage]] = []
    for tick, tempo in project.song.tempo_map or [[0, 500_000]]:
        events.append((tick, 0, MetaMessage("set_tempo", tempo=tempo)))
    for tick, numerator, denominator in project.song.time_signatures:
        events.append((tick, 1, MetaMessage(
            "time_signature", numerator=numerator, denominator=denominator,
        )))
    lyrics = [MetaMessage("cue_marker", text="$Lyrc:1:0:JP")]
    previous_line = None
    lyric_tick = 0
    selected = []
    for note in sorted(project.notes, key=lambda x: (x.start_tick, x.id)):
        kana = lyric_map[note.id]
        if not kana or note.start_tick < 0 or note.end_tick <= note.start_tick:
            raise ValueError("XF export needs an explicit lyric and positive note duration")
        events.extend([
            (note.start_tick, 3, Message("note_on", note=note.midi_note, velocity=96)),
            (note.end_tick, 2, Message("note_off", note=note.midi_note, velocity=0)),
        ])
        if previous_line is not None and previous_line != note.line:
            lyrics.append(MetaMessage("lyrics", text="/", time=note.start_tick - lyric_tick))
            lyric_tick = note.start_tick
        lyrics.append(MetaMessage("lyrics", text=kana, time=note.start_tick - lyric_tick))
        lyric_tick, previous_line = note.start_tick, note.line
        selected.append({
            "note_id": note.id, "line": note.line, "kana": kana,
            "midi_pitch": note.midi_note, "start_tick": note.start_tick,
            "end_tick": note.end_tick, "source": note.source,
        })
    track = MidiTrack()
    previous_tick = 0
    for tick, _, message in sorted(events, key=lambda x: (x[0], x[1])):
        track.append(message.copy(time=tick - previous_tick))
        previous_tick = tick
    midi.tracks.append(track)
    stream = io.BytesIO()
    midi.save(file=stream)
    content = stream.getvalue() + _xf_track(
        b"XFIH", [MetaMessage("cue_marker", text="$XFhd:")], tpb,
    ) + _xf_track(b"XFKM", lyrics, tpb)
    provenance = {
        "schema_version": 1,
        "midi_sha256": hashlib.sha256(content).hexdigest(),
        "parody_applied": project.parody is not None,
        "selected_notes": selected,
        "lyric_layers": project.lyric_layers,
    }
    sidecar = json.dumps(provenance, ensure_ascii=False, indent=1, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.with_suffix(path.suffix + ".provenance.json").write_text(sidecar, encoding="utf-8")
    return path
