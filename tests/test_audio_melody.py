import tomllib
from pathlib import Path

import pytest

from soramimic_video.audio_melody import (
    MelodyNote,
    configured_capabilities,
    read_sheetsage_notes,
)


def test_sheetsage_lab_validation(tmp_path: Path):
    lab = tmp_path / "melody_vocal.lab"
    lab.write_text("0.1\t0.3\t64\n0.4\t0.8\t67\n", encoding="utf-8")
    assert read_sheetsage_notes(lab) == [
        MelodyNote(0.1, 0.3, 64),
        MelodyNote(0.4, 0.8, 67),
    ]
    lab.write_text("0.1\t0.5\t64\n0.4\t0.8\t67\n", encoding="utf-8")
    assert read_sheetsage_notes(lab) == [
        MelodyNote(0.1, 0.4, 64),
        MelodyNote(0.4, 0.8, 67),
    ]


def test_sheetsage_lab_rejects_ambiguous_simultaneous_notes(tmp_path: Path):
    lab = tmp_path / "melody_vocal.lab"
    lab.write_text("0.1\t0.5\t64\n0.1\t0.8\t67\n", encoding="utf-8")
    with pytest.raises(ValueError, match="同時刻"):
        read_sheetsage_notes(lab)


def test_capabilities_are_false_for_unset_local_models(monkeypatch):
    for name in (
        "SORAMIMIC_SHEETSAGE_MODEL_DIR",
        "SORAMIMIC_SHEETSAGE_BASE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert configured_capabilities() == {"sheetsage2": False}


@pytest.mark.parametrize("package", ["mir-eval", "pretty-midi"])
def test_audio_extra_installs_sheetsage_runtime_dependencies(package: str):
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    audio_dependencies = pyproject["project"]["optional-dependencies"]["audio"]
    assert any(dependency.startswith(package) for dependency in audio_dependencies)
