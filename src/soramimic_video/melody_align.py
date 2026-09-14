"""Shared MIDI-note loading helpers for MIDI-based input paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class MelodyNote:
    start_sec: float
    end_sec: float
    midi_note: int


def load_midi_notes(midi_path: Path) -> dict[int, list[MelodyNote]]:
    """Read notes by channel in seconds, with the MIDI tempo map applied."""
    import mido

    mid = mido.MidiFile(str(midi_path), clip=True)
    notes: dict[int, list[MelodyNote]] = {}
    pending: dict[tuple[int, int], float] = {}
    time_sec = 0.0
    for message in mid:
        time_sec += message.time
        if message.type == "note_on" and message.velocity > 0:
            pending[(message.channel, message.note)] = time_sec
        elif message.type in ("note_off", "note_on"):
            start_sec = pending.pop((message.channel, message.note), None)
            if start_sec is not None and time_sec > start_sec:
                notes.setdefault(message.channel, []).append(
                    MelodyNote(start_sec, time_sec, message.note)
                )
    for channel_notes in notes.values():
        channel_notes.sort(key=lambda note: note.start_sec)
    return notes


def monophony_ratio(notes: list[MelodyNote]) -> float:
    """Return the share of adjacent notes that do not overlap."""
    if len(notes) < 2:
        return 1.0
    nonoverlapping = sum(
        1
        for current, following in zip(notes, notes[1:], strict=False)
        if current.end_sec <= following.start_sec + 1e-6
    )
    return nonoverlapping / (len(notes) - 1)


def skyline(notes: list[MelodyNote]) -> list[MelodyNote]:
    """Extract the highest monophonic voice from notes containing chords."""
    result: list[MelodyNote] = []
    for note in sorted(notes, key=lambda value: (value.start_sec, -value.midi_note)):
        if not result:
            result.append(MelodyNote(note.start_sec, note.end_sec, note.midi_note))
            continue
        current = result[-1]
        if note.start_sec < current.end_sec - 1e-6:
            if note.midi_note <= current.midi_note:
                continue
            current.end_sec = note.start_sec
            if current.end_sec <= current.start_sec:
                result.pop()
        result.append(MelodyNote(note.start_sec, note.end_sec, note.midi_note))
    return result
