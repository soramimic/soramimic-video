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
    assert "リャ ry A" in mora
    assert "リョ ry O" in mora
    assert "ヴィ v I" in mora


def test_configured_speaker_id_rejects_non_integer(monkeypatch):
    monkeypatch.setenv(prettypitch.SPEAKER_ID_ENV, "ritsu")
    with pytest.raises(ValueError, match=prettypitch.SPEAKER_ID_ENV):
        prettypitch.configured_speaker_id()


def test_partial_runtime_ignores_application_pythonpath(tmp_path, monkeypatch):
    import sys

    root, leapsinger, _ = _runtime(tmp_path)
    monkeypatch.setenv(prettypitch.ROOT_ENV, str(root))
    monkeypatch.setenv(prettypitch.PYTHON_ENV, sys.executable)
    monkeypatch.setenv(prettypitch.LEAPSINGER_ROOT_ENV, str(leapsinger))
    application_packages = tmp_path / "application-packages"
    application_packages.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(application_packages))
    monkeypatch.setattr(prettypitch, "__file__", str(tmp_path / "prettypitch.py"))
    (tmp_path / "_prettypitch_worker.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "assert os.environ['PYTHONPATH'] not in sys.path\n"
        "Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(b'x' * 64)\n"
    )
    output = prettypitch.run_partial_score("score", tmp_path / "job", duration=1)
    assert output.is_file()


def _native_runtime(tmp_path, monkeypatch):
    nhv = tmp_path / "NHVSing"
    for relative in (
        "nhv_vocoder.py", "model.py", "dsp.py", "config_v3_2.yaml",
        "exported_models/v3_2_1/nhv_v3_2_1.pth",
    ):
        path = nhv / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv(prettypitch.NHVSING_ROOT_ENV, str(nhv))
    return nhv


def test_native_installation_requires_checkpoint(tmp_path, monkeypatch):
    root, leapsinger, python = _runtime(tmp_path)
    nhv = _native_runtime(tmp_path, monkeypatch)
    (root / "checkpoints/nhv_v3_2_1.onnx").unlink()
    assert prettypitch.installation_error(root, python, leapsinger) is None
    (nhv / "exported_models/v3_2_1/nhv_v3_2_1.pth").unlink()
    assert "NHVSing" in prettypitch.installation_error(root, python, leapsinger)


@pytest.mark.parametrize("partial", [False, True])
def test_native_worker_retries_exited_gpu_on_cpu(tmp_path, monkeypatch, partial):
    from types import SimpleNamespace

    root, leapsinger, python = _runtime(tmp_path)
    nhv = _native_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv(prettypitch.ROOT_ENV, str(root))
    monkeypatch.setenv(prettypitch.LEAPSINGER_ROOT_ENV, str(leapsinger))
    monkeypatch.setenv(prettypitch.DEVICE_ENV, "cuda")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            return SimpleNamespace(returncode=75, stdout="", stderr="GPU capacity")
        Path(command[command.index("--output") + 1]).write_bytes(b'x' * 64)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(prettypitch.runproc, "run", run)
    if partial:
        output = prettypitch.run_partial_score("score", tmp_path / "job", duration=1)
    else:
        output = prettypitch.run_prettypitch(
            _project(), tmp_path / "job", lyric_map={}, root=root, python=python,
            leapsinger_root=leapsinger, speaker_id=1,
        )
        assert commands[0][commands[0].index("--output-format") + 1] == "wav"
        assert commands[0][commands[0].index("--speaker-id") + 1] == "1"
    assert output.is_file()
    assert commands[0][1] == "-I"
    assert commands[0][commands[0].index("--nhvsing-root") + 1] == str(nhv)
    assert [cmd[cmd.index("--device") + 1] for cmd in commands] == ["cuda", "cpu"]


def test_worker_does_not_retry_other_failures(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=-15)
    monkeypatch.setattr(prettypitch.runproc, "run", run)
    assert prettypitch._run_worker(["python", "--device", "cuda"], tmp_path).returncode == -15
    assert len(calls) == 1


def test_worker_checks_available_cuda_headroom():
    from types import SimpleNamespace

    from soramimic_video import _prettypitch_worker as worker

    for free, expected in [(1024**3, False), (2 * 1024**3, True)]:
        cuda = SimpleNamespace(
            is_available=lambda: True,
            mem_get_info=lambda dev, available=free: (available, 16 * 1024**3),
        )
        assert worker.cuda_has_capacity(SimpleNamespace(cuda=cuda), "cuda:0") == expected


def test_native_vocoder_preserves_array_contract(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    import numpy as np

    from soramimic_video import _prettypitch_worker as worker

    received = []
    class Native:
        sample_rate = 44100
        hop_size = 256
        def __init__(self, checkpoint, config, device):
            assert device == "cuda:0"
        def infer(self, mel, f0, uv):
            received.append((mel, f0, uv))
            return np.arange(len(f0)*256, dtype=np.float32)
    monkeypatch.setitem(sys.modules, "nhv_vocoder", SimpleNamespace(NHVVocoder=Native))
    session = worker.native_vocoder(tmp_path, "cuda:0", 44100).session
    mel, f0, uv = np.zeros((1, 3, 128)), np.ones((1, 1, 3)), np.zeros((1, 1, 3))
    result = session.run(["waveform"], {"mel": mel, "f0": f0, "uv": uv})[0]
    assert result.shape == (1, 1, 768)
    assert [a.shape for a in received[0]] == [(3, 128), (3,), (3,)]


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_cuda_oom_exits_for_parent_retry_only_on_cuda(monkeypatch, device):
    import sys
    from types import SimpleNamespace

    from soramimic_video import _prettypitch_worker as worker

    class OutOfMemoryError(RuntimeError):
        pass

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(OutOfMemoryError=OutOfMemoryError),
    ))
    monkeypatch.setattr(sys, "argv", [
        "worker", "--root", "pp", "--leapsinger-root", "ls", "--score", "score.ust",
        "--mora-table", "ja.mora", "--output", "out.npy", "--duration", "1",
        "--device", device,
    ])
    def fail(args):
        raise OutOfMemoryError("memory exhausted")
    monkeypatch.setattr(worker, "render_audio", fail)
    if device == "cuda":
        with pytest.raises(SystemExit) as error:
            worker.main()
        assert error.value.code == prettypitch.GPU_RETRY_EXIT_CODE
    else:
        with pytest.raises(OutOfMemoryError):
            worker.main()
