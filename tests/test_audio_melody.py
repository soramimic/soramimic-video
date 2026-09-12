from pathlib import Path

import numpy as np
import pytest

from soramimic_video.audio_melody import (
    MelodyNote,
    PitchEvidence,
    assign_mora_pitches,
    configured_capabilities,
    read_sheetsage_notes,
)
from soramimic_video.mora_align import AlignedMora


def _mora(start: float, end: float, index: int) -> AlignedMora:
    return AlignedMora(0, index, "ア", start, end, 0.0)


def _track(pitches: list[float], family: str) -> PitchEvidence:
    size = len(pitches)
    return PitchEvidence(
        times=np.arange(size) * 0.01,
        midi=np.asarray(pitches, dtype=float),
        confidence=np.ones(size),
        family=family,
    )


def test_sheet_note_wins_and_only_blank_is_recovered():
    moras = [_mora(0.0, 0.1, 0), _mora(0.1, 0.2, 1)]
    notes = [MelodyNote(0.0, 0.1, 72)]
    rmvpe = _track([60.0] * 20, "rmvpe")
    fcpe = _track([60.0] * 20, "fcpe")
    result = assign_mora_pitches(
        moras, notes, rmvpe=rmvpe, fcpe=fcpe, fallback_midi=[55, 56]
    )
    assert [(item.midi_note, item.source) for item in result] == [
        (72, "sheetsage_note"),
        (60, "recovered_note"),
    ]


def test_disagreeing_f0_keeps_mora_as_spoken():
    mora = _mora(0.0, 0.1, 0)
    result = assign_mora_pitches(
        [mora],
        [],
        rmvpe=_track([60.0] * 10, "rmvpe"),
        fcpe=_track([64.0] * 10, "fcpe"),
        fallback_midi=[57],
    )
    assert result[0].midi_note == 57
    assert result[0].source == "spoken"
    assert result[0].confidence is None


def test_sheetsage_lab_validation(tmp_path: Path):
    lab = tmp_path / "melody_vocal.lab"
    lab.write_text("0.1\t0.3\t64\n0.4\t0.8\t67\n", encoding="utf-8")
    assert read_sheetsage_notes(lab) == [
        MelodyNote(0.1, 0.3, 64),
        MelodyNote(0.4, 0.8, 67),
    ]
    lab.write_text("0.1\t0.5\t64\n0.4\t0.8\t67\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重複"):
        read_sheetsage_notes(lab)


def test_capabilities_are_false_for_unset_local_models(monkeypatch):
    for name in (
        "SORAMIMIC_SHEETSAGE_MODEL_DIR",
        "SORAMIMIC_SHEETSAGE_BASE_DIR",
        "SORAMIMIC_RMVPE_ROOT",
        "SORAMIMIC_RMVPE_CHECKPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    capabilities = configured_capabilities()
    assert capabilities["sheetsage2"] is False
    assert capabilities["rmvpe"] is False


def test_explicit_missing_fcpe_checkpoint_disables_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("SORAMIMIC_FCPE_CHECKPOINT", str(tmp_path / "missing.pt"))
    assert configured_capabilities()["fcpe"] is False
