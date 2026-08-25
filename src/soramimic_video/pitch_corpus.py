"""Prepare rights-tracked WAV/MIDI corpora for pitch-model development.

Downloaded corpus files and generated manifests live below ``work/`` and are
never packaged with soramimic-video.  The generated note manifest turns each
melody MIDI into absolute note intervals aligned to the original WAV timeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import wave
import zipfile
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import mido
import requests

REGISTRY_RESOURCE = "pitch_corpora.json"
MANIFEST_SCHEMA_VERSION = 1
SILENCE_LABELS = frozenset({"pau", "sil", "sp"})


class CorpusError(RuntimeError):
    """Raised when a corpus cannot be downloaded, extracted, or validated."""


@dataclass(frozen=True)
class MidiNote:
    start_seconds: float
    end_seconds: float
    midi_pitch: int
    velocity: int
    channel: int


@dataclass(frozen=True)
class PreparedCorpus:
    corpus_id: str
    version: str
    sample_count: int
    note_count: int
    duration_seconds: float
    output_dir: Path
    dataset_metadata: Path
    samples_manifest: Path
    notes_manifest: Path
    license_id: str
    attribution: str
    warnings: tuple[str, ...]


def load_corpus_registry() -> dict[str, dict[str, Any]]:
    """Load and minimally validate the bundled corpus source registry."""
    registry_path = files("soramimic_video").joinpath(REGISTRY_RESOURCE)
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("corpora"), list):
        raise CorpusError("pitch corpus registry has an unsupported schema")
    result: dict[str, dict[str, Any]] = {}
    for row in data["corpora"]:
        corpus_id = row.get("id")
        if not isinstance(corpus_id, str) or not corpus_id or corpus_id in result:
            raise CorpusError("pitch corpus registry contains an invalid or duplicate id")
        result[corpus_id] = row
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(
    path: Path, expected_sha256: str, expected_size_bytes: int | None = None
) -> None:
    if not path.is_file():
        raise CorpusError(f"corpus archive does not exist: {path}")
    if expected_size_bytes is not None and path.stat().st_size != expected_size_bytes:
        raise CorpusError(
            f"corpus archive size mismatch: {path} "
            f"(expected {expected_size_bytes}, got {path.stat().st_size})"
        )
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise CorpusError(
            f"corpus archive checksum mismatch: {path} "
            f"(expected {expected_sha256}, got {actual})"
        )


def download_archive(
    url: str,
    destination: Path,
    expected_sha256: str,
    expected_size_bytes: int | None = None,
) -> Path:
    """Download an archive atomically, or reuse an already verified copy."""
    if destination.exists():
        _verify_archive(destination, expected_sha256, expected_size_bytes)
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(15, 120),
            headers={"User-Agent": "soramimic-video pitch-corpus preparer"},
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if expected_size_bytes is not None and content_length is not None:
                try:
                    response_size = int(content_length)
                except ValueError as exc:
                    raise CorpusError(
                        f"invalid corpus archive Content-Length: {content_length}"
                    ) from exc
                if response_size != expected_size_bytes:
                    raise CorpusError(
                        "corpus archive response size mismatch "
                        f"(expected {expected_size_bytes}, got {content_length})"
                    )
            downloaded = 0
            with partial.open("wb") as target:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        downloaded += len(chunk)
                        if expected_size_bytes is not None and downloaded > expected_size_bytes:
                            raise CorpusError(
                                "corpus archive response exceeded the registered size "
                                f"({expected_size_bytes} bytes)"
                            )
                        target.write(chunk)
        _verify_archive(partial, expected_sha256, expected_size_bytes)
        os.replace(partial, destination)
    except requests.RequestException as exc:
        partial.unlink(missing_ok=True)
        raise CorpusError(f"corpus archive download failed: {url}: {exc}") from exc
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return destination


def _safe_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        unix_mode = member.external_attr >> 16
        has_drive = bool(path.parts and path.parts[0].endswith(":"))
        if path.is_absolute() or has_drive or ".." in path.parts:
            raise CorpusError(f"unsafe path in corpus archive: {member.filename}")
        if stat.S_ISLNK(unix_mode):
            raise CorpusError(f"symbolic link in corpus archive: {member.filename}")
    return members


def extract_archive(archive_path: Path, destination: Path) -> Path:
    """Safely and atomically extract a ZIP archive."""
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(staging, members=_safe_zip_members(archive))
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def read_midi_notes(path: Path) -> list[MidiNote]:
    """Read note intervals from a melody MIDI in absolute seconds."""
    try:
        midi = mido.MidiFile(path)
    except (OSError, EOFError, ValueError) as exc:
        raise CorpusError(f"invalid MIDI file: {path}: {exc}") from exc

    active: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(deque)
    notes: list[MidiNote] = []
    now = 0.0
    for message in midi:
        now += message.time
        if message.type == "note_on" and message.velocity > 0:
            active[(message.channel, message.note)].append((now, message.velocity))
        elif message.type == "note_off" or (
            message.type == "note_on" and message.velocity == 0
        ):
            key = (message.channel, message.note)
            if not active[key]:
                raise CorpusError(f"unmatched note-off in {path} at {now:.6f}s")
            start, velocity = active[key].popleft()
            if now <= start:
                raise CorpusError(f"non-positive note duration in {path} at {start:.6f}s")
            notes.append(
                MidiNote(
                    start_seconds=start,
                    end_seconds=now,
                    midi_pitch=message.note,
                    velocity=velocity,
                    channel=message.channel,
                )
            )
    if any(queue for queue in active.values()):
        raise CorpusError(f"unmatched note-on in MIDI file: {path}")
    if not notes:
        raise CorpusError(f"MIDI file contains no notes: {path}")
    return sorted(notes, key=lambda note: (note.start_seconds, note.end_seconds, note.midi_pitch))


def normalize_monophonic_notes(notes: list[MidiNote]) -> tuple[list[MidiNote], int]:
    """Collapse duplicate same-pitch overlaps and reject polyphonic targets."""
    normalized: list[MidiNote] = []
    collapsed = 0
    for note in notes:
        if normalized and note.start_seconds < normalized[-1].end_seconds - 1e-9:
            previous = normalized[-1]
            if note.channel != previous.channel or note.midi_pitch != previous.midi_pitch:
                raise CorpusError(
                    "overlapping different-pitch notes cannot be a monophonic target: "
                    f"{previous.midi_pitch}@{previous.start_seconds:.6f}s and "
                    f"{note.midi_pitch}@{note.start_seconds:.6f}s"
                )
            normalized[-1] = MidiNote(
                start_seconds=previous.start_seconds,
                end_seconds=max(previous.end_seconds, note.end_seconds),
                midi_pitch=previous.midi_pitch,
                velocity=max(previous.velocity, note.velocity),
                channel=previous.channel,
            )
            collapsed += 1
        else:
            normalized.append(note)
    return normalized, collapsed


def _read_wave_info(path: Path) -> dict[str, int | float]:
    try:
        with wave.open(str(path), "rb") as source:
            frames = source.getnframes()
            sample_rate = source.getframerate()
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            compression = source.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise CorpusError(f"invalid WAV file: {path}: {exc}") from exc
    if compression != "NONE" or not frames or not sample_rate or not channels:
        raise CorpusError(f"unsupported or empty WAV file: {path}")
    return {
        "duration_seconds": frames / sample_rate,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
    }


def _read_phoneme_labels(path: Path) -> tuple[int, float, float]:
    count = 0
    previous_end = 0
    final_end = 0
    final_non_silence_end = 0
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CorpusError(f"invalid phoneme label file: {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        parts = line.split()
        if len(parts) != 3:
            raise CorpusError(f"invalid label at {path}:{line_number}")
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise CorpusError(f"invalid label time at {path}:{line_number}") from exc
        if start < previous_end or end <= start:
            raise CorpusError(f"non-monotonic label at {path}:{line_number}")
        previous_end = end
        final_end = end
        if parts[2] not in SILENCE_LABELS:
            final_non_silence_end = end
        count += 1
    if not count:
        raise CorpusError(f"empty phoneme label file: {path}")
    # HTK label time is expressed in 100-nanosecond units.
    return count, final_end / 10_000_000, final_non_silence_end / 10_000_000


def _relative(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def build_pjs_manifests(
    dataset_root: Path,
    output_dir: Path,
    corpus: dict[str, Any],
    *,
    expected_ids: list[str] | None = None,
) -> PreparedCorpus:
    """Validate PJS and write dataset, sample, and note manifests."""
    if expected_ids is None:
        expected_ids = [f"pjs{number:03d}" for number in range(1, 101)]
    source_root = dataset_root / corpus["archive_root"]
    if not source_root.is_dir():
        raise CorpusError(f"PJS archive root does not exist: {source_root}")

    samples: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_duration = 0.0
    for sample_id in expected_ids:
        sample_dir = source_root / sample_id
        paths = {
            "audio": sample_dir / f"{sample_id}_song.wav",
            "midi": sample_dir / f"{sample_id}.mid",
            "musicxml": sample_dir / f"{sample_id}.musicxml",
            "phoneme_labels": sample_dir / f"{sample_id}.lab",
            "metadata": sample_dir / f"{sample_id}.txt",
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise CorpusError(f"PJS sample {sample_id} is incomplete: {', '.join(missing)}")

        wave_info = _read_wave_info(paths["audio"])
        try:
            ElementTree.parse(paths["musicxml"])
        except (OSError, ElementTree.ParseError) as exc:
            raise CorpusError(f"invalid MusicXML file: {paths['musicxml']}: {exc}") from exc
        notes, collapsed_notes = normalize_monophonic_notes(read_midi_notes(paths["midi"]))
        if collapsed_notes:
            warnings.append(
                f"{sample_id}: collapsed {collapsed_notes} duplicate overlapping MIDI note(s)"
            )
        phoneme_count, label_end, non_silence_end = _read_phoneme_labels(
            paths["phoneme_labels"]
        )
        duration = float(wave_info["duration_seconds"])
        total_duration += duration
        if non_silence_end > duration + 0.05:
            warnings.append(
                f"{sample_id}: non-silence label extends "
                f"{non_silence_end - duration:.3f}s beyond audio; clip labels to audio"
            )
        elif label_end > duration + 0.05:
            warnings.append(
                f"{sample_id}: trailing silence label extends {label_end - duration:.3f}s "
                "beyond audio"
            )
        for index, note in enumerate(notes):
            if note.end_seconds > duration + 0.05:
                raise CorpusError(
                    f"PJS MIDI note extends beyond audio for {sample_id}: "
                    f"{note.end_seconds:.3f}s > {duration:.3f}s"
                )
            note_rows.append(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "sample_id": sample_id,
                    "note_index": index,
                    **asdict(note),
                }
            )

        samples.append(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "corpus_id": corpus["id"],
                "corpus_version": corpus["version"],
                "sample_id": sample_id,
                **{name: _relative(path, output_dir) for name, path in paths.items()},
                **wave_info,
                "phoneme_count": phoneme_count,
                "phoneme_label_end_seconds": label_end,
                "note_count": len(notes),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    samples_manifest = output_dir / "samples.jsonl"
    notes_manifest = output_dir / "notes.jsonl"
    dataset_metadata = output_dir / "dataset.json"
    _write_jsonl(samples_manifest, samples)
    _write_jsonl(notes_manifest, note_rows)
    license_data = corpus["license"]
    summary = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "corpus_id": corpus["id"],
        "name": corpus["name"],
        "version": corpus["version"],
        "source_page": corpus["source_page"],
        "license": license_data,
        "attribution": corpus["attribution"],
        "sample_count": len(samples),
        "note_count": len(note_rows),
        "duration_seconds": total_duration,
        "samples_manifest": samples_manifest.name,
        "notes_manifest": notes_manifest.name,
        "warnings": warnings,
    }
    _write_json(dataset_metadata, summary)
    return PreparedCorpus(
        corpus_id=corpus["id"],
        version=corpus["version"],
        sample_count=len(samples),
        note_count=len(note_rows),
        duration_seconds=total_duration,
        output_dir=output_dir,
        dataset_metadata=dataset_metadata,
        samples_manifest=samples_manifest,
        notes_manifest=notes_manifest,
        license_id=license_data["id"],
        attribution=corpus["attribution"],
        warnings=tuple(warnings),
    )


def prepare_pitch_corpus(
    corpus_id: str,
    root: Path,
    *,
    archive_path: Path | None = None,
    download: bool = True,
) -> PreparedCorpus:
    """Download, extract, validate, and index a registered pitch corpus."""
    registry = load_corpus_registry()
    if corpus_id not in registry:
        available = ", ".join(sorted(registry))
        raise CorpusError(f"unknown pitch corpus {corpus_id!r}; available: {available}")
    corpus = registry[corpus_id]
    if corpus.get("format") != "pjs-v1":
        raise CorpusError(f"unsupported pitch corpus format: {corpus.get('format')}")

    root = root.resolve()
    expected_sha256 = corpus["archive_sha256"]
    expected_size_bytes = corpus.get("archive_size_bytes")
    if archive_path is None:
        archive_path = root / "downloads" / corpus["archive_filename"]
        if download:
            download_archive(
                corpus["download_url"],
                archive_path,
                expected_sha256,
                expected_size_bytes,
            )
        else:
            _verify_archive(archive_path, expected_sha256, expected_size_bytes)
    else:
        archive_path = archive_path.resolve()
        _verify_archive(archive_path, expected_sha256, expected_size_bytes)

    dataset_root = root / "datasets" / f"{corpus_id}-v{corpus['version']}"
    extract_archive(archive_path, dataset_root)
    output_dir = root / "prepared" / f"{corpus_id}-v{corpus['version']}"
    return build_pjs_manifests(dataset_root, output_dir, corpus)
