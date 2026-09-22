from __future__ import annotations

from pathlib import Path

import pytest

from soramimic_video import prettypitch
from soramimic_video.project import Note, Project, SongInfo


def _note(note_id: int, midi: int, start: float, end: float, kana: str) -> Note:
    return Note(
        id=note_id,
        midi_note=midi,
        start_tick=0,
        end_tick=0,
        start_sec=start,
        end_sec=end,
        line=0,
        surface=kana,
        kana=kana,
        raw="",
    )


def _project() -> Project:
    return Project(
        song=SongInfo(
            midi_path="",
            ticks_per_beat=480,
            tempo_map=[[0, 500000], [1920, 400000]],
        ),
        notes=[
            _note(0, 60, 0.25, 0.75, "リュ"),
            _note(1, 62, 1.0, 1.5, "ヴィ"),
        ],
    )


def _runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "PrettyPitch"
    leapsinger = tmp_path / "LeapSinger"
    python = root / ".venv" / "bin" / "python"
    for path in (
        root / "svs" / "render.py",
        root / "dict" / "ja.mora",
        root / "checkpoints" / "f0_3singer.pt",
        root / "checkpoints" / "cons_dur_3singer.pt",
        root / "checkpoints" / "nhv_v3_2_1.onnx",
        root / "checkpoints" / "leapsinger" / "3speaker_gan2d.pth",
        leapsinger / "infer.py",
        python,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("リュ ry u\n" if path.name == "ja.mora" else "", encoding="utf-8")
    python.chmod(0o755)
    return root, leapsinger, python


def test_build_ust_uses_absolute_seconds_across_tempo_changes():
    ust = prettypitch.build_ust(_project(), {0: "リュ", 1: "ヴィ"}, transpose=2)

    # 120 BPMでは1秒=960tick。tempo_mapの変化には影響されない。
    assert "Length=240\nLyric=R" in ust
    assert "Length=480\nLyric=リュ\nNoteNum=62" in ust
    assert "Length=240\nLyric=R" in ust
    assert "Length=480\nLyric=ヴィ\nNoteNum=64" in ust


def test_installation_error_requires_explicit_runtime(monkeypatch):
    monkeypatch.delenv(prettypitch.ROOT_ENV, raising=False)
    assert prettypitch.ROOT_ENV in (prettypitch.installation_error() or "")


def test_configured_python_preserves_virtualenv_symlink(tmp_path):
    root = tmp_path / "PrettyPitch"
    system_python = tmp_path / "python3"
    system_python.write_text("", encoding="utf-8")
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(system_python)

    assert prettypitch.configured_python(root) == venv_python.absolute()
    assert prettypitch.configured_python(root) != system_python.resolve()


def test_run_prettypitch_builds_score_and_uses_external_python(tmp_path, monkeypatch):
    root, leapsinger, python = _runtime(tmp_path)
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        out = Path(command[command.index("-o") + 1])
        out.write_bytes(b"RIFF-test")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(prettypitch.runproc, "run", fake_run)
    progress: list[float] = []
    output = prettypitch.run_prettypitch(
        _project(),
        tmp_path / "job",
        lyric_map={0: "リュ", 1: "ヴィ"},
        root=root,
        python=python,
        leapsinger_root=leapsinger,
        speaker_id=2,
        device="cpu",
        progress_cb=progress.append,
    )

    assert output == (tmp_path / "job" / "neutrino" / "vocal.wav").resolve()
    assert output.read_bytes() == b"RIFF-test"
    assert captured["command"][:3] == [str(python), "-m", "svs.render"]
    assert captured["command"][captured["command"].index("--spk_id") + 1] == "2"
    assert captured["command"][captured["command"].index("--device") + 1] == "cpu"
    assert captured["kwargs"]["cwd"] == root
    assert progress == [0.0, 1.0]
    mora = (tmp_path / "job" / "prettypitch" / "ja.mora").read_text(encoding="utf-8")
    assert mora.count("リュ ry u") == 1
    assert "リョ ry O" in mora
    assert "ヴィ v I" in mora


def test_configured_speaker_id_rejects_non_integer(monkeypatch):
    monkeypatch.setenv(prettypitch.SPEAKER_ID_ENV, "ritsu")
    with pytest.raises(ValueError, match=prettypitch.SPEAKER_ID_ENV):
        prettypitch.configured_speaker_id()
