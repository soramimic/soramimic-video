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
    result = vv._silence_long_pauses(content, data)
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
    assert vv._silence_long_pauses(content, query(leading)) == content


def test_all_pause_query_becomes_silence():
    content, original = audio()
    data = {"f0": [0.0] * 300,
            "phonemes": [{"phoneme": "pau", "frame_length": 300}]}
    params, samples = pcm(vv._silence_long_pauses(content, data))
    assert params.nframes == len(original)
    assert not np.any(samples)


@pytest.mark.parametrize("data", [{}, {"f0": [], "phonemes": []},
    {"f0": [0] * 300, "phonemes": [{"phoneme": "pau", "frame_length": 400}]}])
def test_missing_or_unaligned_query_is_not_used_to_mute_audio(data):
    content, _ = audio()
    assert vv._silence_long_pauses(content, data) == content


def test_mismatched_wave_duration_is_not_muted():
    content, _ = audio(frames=200)
    assert vv._silence_long_pauses(content, query()) == content


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


@pytest.mark.parametrize("rate,channels", [(24000, 1), (48000, 2), (44100, 1)])
def test_internal_pause_is_muted_but_phonemes_and_both_margins_are_exact(rate, channels):
    content, original = audio(rate=rate, channels=channels)
    data = query()
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 50},
        {"phoneme": "pau", "frame_length": 100},
        {"phoneme": "sh", "frame_length": 10},
        {"phoneme": "i", "frame_length": 140},
    ]
    params, samples = pcm(vv._silence_long_pauses(content, data))
    left = round((50 / vv.FRAME_RATE + .15) * rate)
    right = round((150 / vv.FRAME_RATE - .15) * rate)
    fade = round(.01 * rate)
    assert params.nframes == len(original)
    assert np.array_equal(samples[:left], original[:left])
    assert np.array_equal(samples[right:], original[right:])
    assert not np.any(samples[left + fade:right - fade])
    assert np.all(np.diff(samples[left:left + fade, 0].astype(int)) <= 0)
    assert np.all(np.diff(samples[right - fade:right, 0].astype(int)) >= 0)


def test_trailing_pause_keeps_decay_then_fades_to_silence():
    content, original = audio()
    data = query()
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 100},
        {"phoneme": "pau", "frame_length": 200},
    ]
    _, samples = pcm(vv._silence_long_pauses(content, data))
    kept = round((100 / vv.FRAME_RATE + .15) * 24000)
    assert np.array_equal(samples[:kept], original[:kept])
    assert not np.any(samples[kept + 240:])


@pytest.mark.parametrize("pause_frames", [1, 20, 46, 47])
def test_half_second_threshold_preserves_short_internal_rests(pause_frames):
    content, original = audio()
    data = query()
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 100},
        {"phoneme": "pau", "frame_length": pause_frames},
        {"phoneme": "s", "frame_length": 10},
        {"phoneme": "a", "frame_length": 190 - pause_frames},
    ]
    result = vv._silence_long_pauses(content, data)
    if pause_frames < 47:
        assert result == content
    else:
        _, samples = pcm(result)
        assert not np.any(samples[round(1.3 * 24000):round(1.4 * 24000)])
        assert np.array_equal(samples[-24000:], original[-24000:])


def test_contiguous_pauses_are_combined_even_across_zero_length_phonemes():
    content, _ = audio()
    data = query()
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 100},
        {"phoneme": "pau", "frame_length": 30},
        {"phoneme": "sh", "frame_length": 0},
        {"phoneme": "pau", "frame_length": 30},
        {"phoneme": "a", "frame_length": 140},
    ]
    _, samples = pcm(vv._silence_long_pauses(content, data))
    assert not np.any(samples[round(1.3 * 24000):round(1.5 * 24000)])


@pytest.mark.parametrize("bad_length", [-1, 100.5, "100", None, True])
def test_invalid_lengths_never_mute_audio(bad_length):
    content, _ = audio()
    data = query()
    data["phonemes"][0]["frame_length"] = bad_length
    assert vv._silence_long_pauses(content, data) == content


def test_partial_synthesis_mutes_internal_rest_in_a_nonzero_retained_range(monkeypatch):
    data = query(leading=0)
    data["phonemes"] = [
        {"phoneme": "a", "frame_length": 50},
        {"phoneme": "pau", "frame_length": 100},
        {"phoneme": "sh", "frame_length": 10},
        {"phoneme": "a", "frame_length": 140},
    ]
    content, _ = audio(frames=280)
    monkeypatch.setattr(vv, "_query_score", lambda *args: data)
    monkeypatch.setattr(vv, "_synthesize_frame_query", lambda *args: content)
    chunk = vv.ScoreChunk(100, [{"key": 60, "frame_length": 300, "lyric": "シ"}])
    params, samples = pcm(vv._partial_chunk(chunk, "test", 6000, [(120, 400)]))
    assert params.nframes == 300 * 256
    assert not np.any(samples[24000:30000])
    assert np.all(samples[-24000:] == 1000)
