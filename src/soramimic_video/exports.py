"""Portable score and isolated synthesized-vocal downloads."""

from __future__ import annotations

import shutil
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

from .musicxml import build_musicxml
from .project import Project
from .synthesize import build_lyric_map, vocal_path
from .xfexport import export_xf_midi

EXPORT_DIR = "exports"
# Fixed names also form the API allowlist. Never accept a caller-provided path.
FORMATS = {
    "musicxml": ("score.musicxml", "application/vnd.recordare.musicxml+xml"),
    "midi": ("lyrics.mid", "audio/midi"),
    "xf": ("lyrics.xf.mid", "audio/midi"),
    "vocal": ("vocal.wav", "audio/wav"),
}


def result_path(directory: Path, kind: str) -> Path | None:
    if kind not in FORMATS:
        return None
    folder = directory / EXPORT_DIR
    path = folder / FORMATS[kind][0]
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    return path if path.stat().st_size else None


def export_lyric_midi(project: Project, path: Path) -> Path:
    """Write a melody SMF with UTF-8 lyric meta events at note onsets."""
    midi = MidiFile(type=1, ticks_per_beat=project.song.ticks_per_beat, charset="utf8")
    events: list[tuple[int, int, Message | MetaMessage]] = []
    for tick, tempo in project.song.tempo_map or [[0, 500_000]]:
        events.append((tick, 0, MetaMessage("set_tempo", tempo=tempo)))
    for tick, num, den in project.song.time_signatures:
        events.append((tick, 0, MetaMessage("time_signature", numerator=num, denominator=den)))
    lyric_map = build_lyric_map(project)
    previous_line = None
    for note in sorted(project.notes, key=lambda note: (note.start_tick, note.id)):
        if note.start_tick < 0 or note.end_tick <= note.start_tick:
            raise ValueError("MIDI export needs a nonnegative start and positive note duration")
        lyric = lyric_map[note.id]
        if lyric:
            if previous_line is not None and previous_line != note.line:
                lyric = "\n" + lyric
            events.append((note.start_tick, 2, MetaMessage("lyrics", text=lyric)))
            previous_line = note.line
        events.extend([
            (note.start_tick, 3, Message("note_on", note=note.midi_note, velocity=96)),
            (note.end_tick, 1, Message("note_off", note=note.midi_note, velocity=0)),
        ])
    track = MidiTrack()
    previous = 0
    for tick, _, event in sorted(events, key=lambda item: (item[0], item[1])):
        track.append(event.copy(time=tick - previous))
        previous = tick
    midi.tracks.append(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)
    return path


def export_file(
    project: Project, directory: Path, kind: str, output: Path,
    *, vocal_source: Path | None = None,
) -> Path:
    """Export original-pitch notes and current lyrics, or the synthesized vocal."""
    if kind not in FORMATS:
        raise ValueError(f"Unknown export format: {kind}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if kind == "musicxml":
        output.write_text(build_musicxml(
            project, build_lyric_map(project), for_synthesis=False,
        ), encoding="utf-8")
    elif kind == "midi":
        export_lyric_midi(project, output)
    elif kind == "xf":
        # Full provenance can contain uploaded text and local source metadata.
        export_xf_midi(project, output, provenance=False)
    else:
        source = vocal_source if vocal_source is not None else vocal_path(directory)
        if not source.is_file():
            raise FileNotFoundError("歌声WAVがありません。先に歌唱合成を実行してください")
        if source.resolve() != output.resolve():
            shutil.copyfile(source, output)
    return output


def save_results(
    project: Project, directory: Path, *, vocal_source: Path | None = None,
) -> None:
    for kind, (name, _) in FORMATS.items():
        export_file(
            project, directory, kind, directory / EXPORT_DIR / name, vocal_source=vocal_source,
        )
