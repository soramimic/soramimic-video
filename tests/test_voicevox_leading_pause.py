import copy
import io
import wave

import numpy as np
import pytest

from soramimic_video import voicevox as vv


def query(leading=200, total=300):
    return {
        "f0": [0.0] * total,
        "volume": [0.1] * total,
        "phonemes": [
            {"phoneme": "pau", "frame_length": leading},
            {"phoneme": "sh", "frame_length": 10},
            {"phoneme": "a", "frame_length": total - leading - 10},
        ],
    }


def audio(frames=300, rate=24000, channels=1):
    samples = np.full((round(frames * rate / vv.FRAME_RATE), channels), 1000, "<i2")
    if channels == 2:
        samples[:, 1] = -700
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(samples.tobytes())
    return buffer.getvalue(), samples


def pcm(content):
    with wave.open(io.BytesIO(content)) as source:
        params = source.getparams()
        samples = np.frombuffer(source.readframes(source.getnframes()), "<i2")
    return params, samples.reshape(-1, params.nchannels)


@pytest.mark.parametrize("rate,channels", [(24000, 1), (48000, 2), (44100, 1)])
def test_silences_leading_noise_and_preserves_phonemes_and_duration(rate, channels):
    content, original = audio(rate=rate, channels=channels)
    data = query()
    saved = copy.deepcopy(data)
    result = vv._silence_leading_pause(content, data)
    params, samples = pcm(result)
    assert params.nframes == len(original)
    assert params.framerate == rate
    assert not np.any(samples[:rate])
    # All phonemes, including an unvoiced consonant and its preroll, are exact PCM.
    kept = round((200 / vv.FRAME_RATE - .15) * rate)
    assert np.array_equal(samples[kept:], original[kept:])
    fade = samples[kept - round(.01 * rate):kept, 0]
    assert np.all(np.diff(fade.astype(int)) >= 0)
    assert data == saved


@pytest.mark.parametrize("leading", [0, 2, 20])
def test_short_or_absent_initial_pause_is_unchanged(leading):
    content, _ = audio()
    assert vv._silence_leading_pause(content, query(leading)) == content


def test_later_pauses_are_not_muted():
    content, original = audio()
    data = query()
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 10},
        {"phoneme": "pau", "frame_length": 280},
        {"phoneme": "a", "frame_length": 10},
    ]
    assert vv._silence_leading_pause(content, data) == content
    assert np.any(original)


def test_all_pause_query_becomes_silence():
    content, original = audio()
    data = {"f0": [0.0] * 300,
            "phonemes": [{"phoneme": "pau", "frame_length": 300}]}
    params, samples = pcm(vv._silence_leading_pause(content, data))
    assert params.nframes == len(original)
    assert not np.any(samples)


@pytest.mark.parametrize("data", [{}, {"f0": [], "phonemes": []},
    {"f0": [0] * 300, "phonemes": [{"phoneme": "pau", "frame_length": 400}]}])
def test_missing_or_unaligned_query_is_not_used_to_mute_audio(data):
    content, _ = audio()
    assert vv._silence_leading_pause(content, data) == content


def test_mismatched_wave_duration_is_not_muted():
    content, _ = audio(frames=200)
    assert vv._silence_leading_pause(content, query()) == content


def test_normal_synthesis_cleans_the_returned_waveform(monkeypatch):
    content, _ = audio()
    monkeypatch.setattr(vv, "_query_score", lambda *args: query())
    monkeypatch.setattr(vv, "_synthesize_frame_query", lambda *args: content)
    _, samples = pcm(vv._synthesize_chunk("test", "test", 6000, 6000, {}))
    assert not np.any(samples[:24000])
    assert np.all(samples[-24000:] == 1000)


def test_partial_synthesis_cleans_the_retained_waveform(monkeypatch):
    content, _ = audio()
    monkeypatch.setattr(vv, "_query_score", lambda *args: query())
    monkeypatch.setattr(vv, "_synthesize_frame_query", lambda *args: content)
    chunk = vv.ScoreChunk(0, [{"key": 60, "frame_length": 300, "lyric": "シ"}])
    _, samples = pcm(vv._partial_chunk(chunk, "test", 6000, [(0, 300)]))
    assert not np.any(samples[:24000])
    assert np.all(samples[-24000:] == 1000)
