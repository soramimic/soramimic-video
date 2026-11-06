"""KanaWhisper inference adapter tests without loading model weights."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from soramimic_video import kana_whisper


def test_model_available_requires_a_complete_local_snapshot(monkeypatch, tmp_path):
    def snapshot(model, *, revision, local_files_only):
        assert model == kana_whisper.KANA_WHISPER_MODEL
        assert revision == kana_whisper.KANA_WHISPER_REVISION
        assert local_files_only is True
        return str(tmp_path)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        snapshot_download=snapshot,
    ))
    files = ("config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json")
    for name in files:
        assert kana_whisper.model_available() is False
        (tmp_path / name).touch()
    assert kana_whisper.model_available() is True


def test_model_available_tolerates_a_missing_snapshot(monkeypatch):
    def missing(*_args, **_kwargs):
        raise OSError("snapshot is not cached")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        snapshot_download=missing,
    ))
    assert kana_whisper.model_available() is False


@pytest.fixture
def local_inference(monkeypatch, tmp_path):
    audio = tmp_path / "vocals.wav"
    waveform = np.arange(48_000, dtype=np.float32)
    calls, loads, devices = [], [], []

    def load(path, **kwargs):
        loads.append((path, kwargs))
        return waveform, 16_000

    def transcribe(samples, **kwargs):
        calls.append((samples.copy(), kwargs))
        return {"text": f" カナ{len(calls)}\n"}

    def model(device):
        devices.append(device)
        return transcribe

    monkeypatch.setitem(sys.modules, "librosa", SimpleNamespace(load=load))
    monkeypatch.setattr(kana_whisper, "_load_model", model)
    return SimpleNamespace(audio=audio, waveform=waveform, calls=calls,
                           loads=loads, devices=devices)


def test_local_windows_keep_order_and_clip_to_the_audio(local_inference):
    setup = local_inference
    results = kana_whisper._transcribe_kana_windows_local(
        setup.audio, [(0.25, 0.5), (2.5, 3.25)], "cpu",
    )

    assert results == ["カナ1", "カナ2"]
    assert setup.loads == [(str(setup.audio), {"sr": 16_000, "mono": True})]
    assert setup.devices == ["cpu"]
    np.testing.assert_array_equal(setup.calls[0][0], setup.waveform[4_000:8_000])
    np.testing.assert_array_equal(setup.calls[1][0], setup.waveform[40_000:])
    assert all(kwargs == {"generate_kwargs": {"language": "ja", "task": "transcribe"}}
               for _, kwargs in setup.calls)


@pytest.mark.parametrize("window", [(3.0, 4.0), (3.5, 4.0), (1.0, 1.0)])
def test_empty_local_window_fails_before_inference(local_inference, window):
    setup = local_inference
    with pytest.raises(ValueError, match="outside the input audio"):
        kana_whisper._transcribe_kana_windows_local(setup.audio, [window], "cpu")
    assert setup.calls == []


def test_excessive_window_count_does_not_load_audio_or_model(local_inference):
    setup = local_inference
    windows = [(0.0, 0.1)] * (kana_whisper.KANA_MAX_WINDOWS + 1)
    with pytest.raises(ValueError, match="window count"):
        kana_whisper._transcribe_kana_windows_local(setup.audio, windows, "cpu")
    assert setup.loads == setup.devices == setup.calls == []


def test_cancellation_stops_before_the_next_window(local_inference):
    setup = local_inference

    def cancel():
        if setup.calls:
            raise RuntimeError("job canceled")

    with pytest.raises(RuntimeError, match="job canceled"):
        kana_whisper._transcribe_kana_windows_local(
            setup.audio, [(0.0, 0.5), (0.5, 1.0)], "cpu", cancel_check=cancel,
        )
    assert len(setup.calls) == 1
