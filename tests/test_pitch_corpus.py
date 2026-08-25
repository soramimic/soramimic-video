import json
import wave
import zipfile
from pathlib import Path

import mido
import pytest

from soramimic_video.pitch_corpus import (
    CorpusError,
    MidiNote,
    build_pjs_manifests,
    download_archive,
    extract_archive,
    load_corpus_registry,
    normalize_monophonic_notes,
    read_midi_notes,
    sha256_file,
)


def _write_wav(path: Path, duration_seconds: float = 2.0) -> None:
    sample_rate = 8_000
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"\0\0" * int(sample_rate * duration_seconds))


def _write_midi(path: Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    tempo = mido.MidiTrack()
    tempo.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    melody = mido.MidiTrack()
    melody.append(mido.Message("note_on", note=60, velocity=80, time=480))
    melody.append(mido.Message("note_off", note=60, velocity=0, time=480))
    midi.tracks.extend((tempo, melody))
    midi.save(path)


def _write_pjs_sample(root: Path, sample_id: str) -> None:
    sample = root / "PJS_corpus_ver1.1" / sample_id
    sample.mkdir(parents=True)
    _write_wav(sample / f"{sample_id}_song.wav")
    _write_midi(sample / f"{sample_id}.mid")
    (sample / f"{sample_id}.musicxml").write_text(
        '<?xml version="1.0"?><score-partwise version="3.1"/>', encoding="utf-8"
    )
    (sample / f"{sample_id}.lab").write_text(
        "0 5000000 pau\n5000000 10000000 a\n10000000 25000000 pau\n",
        encoding="ascii",
    )
    (sample / f"{sample_id}.txt").write_text("key:C major\n", encoding="utf-8")


def _corpus_spec() -> dict:
    return {
        "id": "pjs",
        "name": "PJS test corpus",
        "version": "1.1",
        "archive_root": "PJS_corpus_ver1.1",
        "source_page": "https://example.test/pjs",
        "license": {"id": "CC-BY-SA-4.0", "url": "https://example.test/license"},
        "attribution": "PJS test attribution",
    }


def test_registry_records_verified_pjs_source_and_license():
    pjs = load_corpus_registry()["pjs"]

    assert pjs["version"] == "1.1"
    assert pjs["license"]["id"] == "CC-BY-SA-4.0"
    assert pjs["source_page"].startswith("https://sites.google.com/")
    assert pjs["archive_size_bytes"] == 275_179_158
    assert len(pjs["archive_sha256"]) == 64


def test_sha256_file(tmp_path):
    path = tmp_path / "payload"
    path.write_bytes(b"abc")

    assert sha256_file(path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_download_archive_reuses_only_verified_existing_file(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"abc")
    digest = sha256_file(path)

    assert download_archive("https://invalid.test/unused", path, digest, 3) == path
    with pytest.raises(CorpusError, match="size mismatch"):
        download_archive("https://invalid.test/unused", path, digest, 4)


@pytest.mark.parametrize("unsafe_name", ["../escaped.txt", "..\\escaped.txt", "C:/escaped.txt"])
def test_extract_archive_rejects_parent_traversal(tmp_path, unsafe_name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(unsafe_name, "bad")

    with pytest.raises(CorpusError, match="unsafe path"):
        extract_archive(archive, tmp_path / "dataset")
    assert not (tmp_path / "escaped.txt").exists()


def test_read_midi_notes_returns_absolute_intervals(tmp_path):
    path = tmp_path / "melody.mid"
    _write_midi(path)

    notes = read_midi_notes(path)
    assert len(notes) == 1
    assert notes[0].start_seconds == pytest.approx(0.5)
    assert notes[0].end_seconds == pytest.approx(1.0)
    assert notes[0].midi_pitch == 60
    assert notes[0].velocity == 80
    assert notes[0].channel == 0


def test_normalize_monophonic_notes_collapses_duplicate_overlap():
    notes, collapsed = normalize_monophonic_notes(
        [
            MidiNote(0.5, 1.0, 60, 70, 0),
            MidiNote(0.75, 1.25, 60, 80, 0),
            MidiNote(1.25, 1.5, 60, 75, 0),
        ]
    )

    assert collapsed == 1
    assert notes == [
        MidiNote(0.5, 1.25, 60, 80, 0),
        MidiNote(1.25, 1.5, 60, 75, 0),
    ]


def test_normalize_monophonic_notes_rejects_polyphony():
    with pytest.raises(CorpusError, match="different-pitch"):
        normalize_monophonic_notes(
            [
                MidiNote(0.5, 1.0, 60, 80, 0),
                MidiNote(0.75, 1.25, 64, 80, 0),
            ]
        )


def test_build_pjs_manifests_validates_and_indexes_audio_midi_pairs(tmp_path):
    dataset = tmp_path / "dataset"
    for sample_id in ("pjs001", "pjs002"):
        _write_pjs_sample(dataset, sample_id)
    output = tmp_path / "prepared"

    prepared = build_pjs_manifests(
        dataset,
        output,
        _corpus_spec(),
        expected_ids=["pjs001", "pjs002"],
    )

    assert prepared.sample_count == 2
    assert prepared.note_count == 2
    assert prepared.duration_seconds == pytest.approx(4.0)
    assert len(prepared.warnings) == 2
    metadata = json.loads(prepared.dataset_metadata.read_text(encoding="utf-8"))
    samples = [json.loads(line) for line in prepared.samples_manifest.read_text().splitlines()]
    notes = [json.loads(line) for line in prepared.notes_manifest.read_text().splitlines()]
    assert metadata["license"]["id"] == "CC-BY-SA-4.0"
    assert metadata["note_count"] == 2
    assert samples[0]["audio"].endswith("pjs001/pjs001_song.wav")
    assert (prepared.output_dir / samples[0]["audio"]).is_file()
    assert samples[0]["sample_rate_hz"] == 8_000
    assert samples[0]["note_count"] == 1
    assert notes[0] == {
        "channel": 0,
        "end_seconds": 1.0,
        "midi_pitch": 60,
        "note_index": 0,
        "sample_id": "pjs001",
        "schema_version": 1,
        "start_seconds": 0.5,
        "velocity": 80,
    }


def test_build_pjs_manifests_rejects_incomplete_sample(tmp_path):
    dataset = tmp_path / "dataset"
    (dataset / "PJS_corpus_ver1.1" / "pjs001").mkdir(parents=True)

    with pytest.raises(CorpusError, match="is incomplete"):
        build_pjs_manifests(
            dataset,
            tmp_path / "prepared",
            _corpus_spec(),
            expected_ids=["pjs001"],
        )
