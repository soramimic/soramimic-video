from __future__ import annotations

import copy
import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from soramimic_video import hybrid, runproc
from soramimic_video import voicevox as vv


def score(*notes):
    return {"notes": [
        {"key": key, "frame_length": frames, "lyric": "ラ" if key is not None else ""}
        for key, frames in notes
    ]}


def wav(frames, value=1000, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.full(frames * vv._samples_per_frame(rate), value, "<i2").tobytes())
    return buffer.getvalue()


def test_phrases_select_whole_sung_group_but_stop_at_rest():
    data = score((None, 20), (60, 30), (80, 25), (None, 5), (65, 40),
                 (None, 12), (62, 50), (None, 70))
    plan = hybrid.plan_phrases(data)
    assert plan["selected_phrases"] == 1
    assert plan["intervals"] == pytest.approx([(10 / vv.FRAME_RATE, 126 / vv.FRAME_RATE)])
    assert {1, 2, 4} <= set(plan["context_notes"])
    # The next phrase is context, not a replacement.
    assert plan["intervals"][0][1] < 132 / vv.FRAME_RATE
    for start, end in plan["skip_frames"]:
        assert start / vv.FRAME_RATE >= plan["intervals"][0][0] + .09
        assert end / vv.FRAME_RATE <= plan["intervals"][0][1] - .09


def test_adjacent_selected_phrases_merge_in_shared_rest():
    plan = hybrid.plan_phrases(score((None, 2), (50, 30), (None, 12), (80, 30), (None, 20)))
    assert plan["selected_phrases"] == 2
    assert len(plan["intervals"]) == 1


def test_range_endpoints_are_not_replaced():
    plan = hybrid.plan_phrases(score((None, 2), (54, 40), (78, 40), (None, 20)))
    assert plan["intervals"] == plan["skip_frames"] == plan["context_notes"] == []


def test_partial_ust_preserves_frames_and_complete_context_lyrics():
    data = score((None, 2), (60, 47), (None, 20), (80, 80), (None, 22))
    data["notes"][3]["lyric"] = "リュ"
    ust = hybrid.build_partial_ust(data, [3])
    assert "Lyric=リュ\nNoteNum=80" in ust
    assert "Lyric=ラ" not in ust
    lengths = [float(line.split("=")[1]) for line in ust.splitlines() if line.startswith("Length=")]
    assert len(lengths) == 3
    assert lengths[0] / 960 == pytest.approx(69 / vv.FRAME_RATE)
    assert sum(lengths) / 960 == pytest.approx(171 / vv.FRAME_RATE, abs=1e-9)


def test_slice_query_cuts_inside_phonemes_without_mutating_original():
    query = {"f0": list(range(10)), "volume": [0.2] * 10, "outputSamplingRate": 24000,
             "phonemes": [{"phoneme": "k", "frame_length": 4, "note_id": "a"},
                          {"phoneme": "a", "frame_length": 6, "note_id": "b"}]}
    sliced = vv.slice_frame_query(query, 2, 7)
    assert sliced["f0"] == [2, 3, 4, 5, 6]
    assert sliced["volume"] == [0.2] * 5
    assert sliced["phonemes"] == [dict(query["phonemes"][0], frame_length=2),
                                   dict(query["phonemes"][1], frame_length=3)]
    assert sliced["outputSamplingRate"] == 24000
    assert query["phonemes"][0]["frame_length"] == 4
    query["phonemes"][1]["frame_length"] = 5
    with pytest.raises(ValueError, match="一致"):
        vv.slice_frame_query(query, 2, 7)


def test_partial_decode_queries_full_score_and_places_remaining_audio(monkeypatch):
    data = score((None, 2), (60, 70), (None, 28))
    calls = []

    def post(url, *, params, json, timeout):
        assert params["speaker"] == 6000
        calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("sing_frame_audio_query"):
            assert json == data
            return SimpleNamespace(status_code=200, json=lambda: {
                "f0": [0] * 100, "volume": [0.5] * 100,
                "phonemes": [{"phoneme": "a", "frame_length": 100}],
            })
        return SimpleNamespace(status_code=200, content=wav(len(json["f0"])))

    monkeypatch.setattr(vv.requests, "post", post)
    result = vv.synthesize_partial_score(
        data, engine_url="http://test", style_id=6000, skip_frames=[(20, 80)],
    )
    with wave.open(io.BytesIO(result)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), "<i2")
    assert len(calls) == 3
    assert len(samples) == 100 * 256
    assert np.all(samples[:20 * 256] == 1000)
    assert np.all(samples[20 * 256:80 * 256] == 0)
    assert np.all(samples[80 * 256:] == 1000)


def test_decoder_failure_splits_only_waveform_windows_and_keeps_the_query(monkeypatch):
    query = {"f0": [220. + i for i in range(100)], "volume": [.2 + i / 1000 for i in range(100)],
             "outputSamplingRate": 24000, "volumeScale": .9,
             "phonemes": [{"phoneme": "a", "frame_length": 60, "note_id": "n1"},
                          {"phoneme": "i", "frame_length": 40, "note_id": "n2"}]}
    before = copy.deepcopy(query)
    sizes = []

    def post(url, *, params, json, timeout):
        assert url.endswith("frame_synthesis") and params == {"speaker": 6000}
        start = round(json["f0"][0] - 220.)
        length = len(json["f0"])
        sizes.append(length)
        assert json["f0"] == query["f0"][start:start + length]
        assert json["volume"] == query["volume"][start:start + length]
        assert sum(p["frame_length"] for p in json["phonemes"]) == length
        assert all(p["note_id"] in {"n1", "n2"} for p in json["phonemes"])
        assert json["outputSamplingRate"] == 24000 and json["volumeScale"] == .9
        return SimpleNamespace(status_code=500, text="decoder failure") if length > 25 else \
            SimpleNamespace(status_code=200, content=wav(length))

    monkeypatch.setattr(vv.requests, "post", post)
    content = vv._synthesize_frame_query("http://test", 6000, query)
    with wave.open(io.BytesIO(content)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), "<i2")
    assert len(samples) == 100 * 256 and np.all(samples == 1000)
    assert sizes[0] == 100 and min(sizes) <= 25
    assert query == before


def test_partial_decoder_recovery_preserves_omitted_frames(monkeypatch):
    monkeypatch.setattr(vv, "_query_score", lambda *args: {
        "f0": [220.] * 100, "volume": [.5] * 100,
        "phonemes": [{"phoneme": "a", "frame_length": 100}],
    })
    monkeypatch.setattr(vv.requests, "post", lambda *args, json, **kwargs:
        SimpleNamespace(status_code=500, text="decoder failure") if len(json["f0"]) > 10 else
        SimpleNamespace(status_code=200, content=wav(len(json["f0"]))))
    result = vv.synthesize_partial_score(
        score((None, 2), (60, 70), (None, 28)), engine_url="http://test", style_id=6000,
        skip_frames=[(20, 80)],
    )
    with wave.open(io.BytesIO(result)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), "<i2")
    assert len(samples) == 100 * 256
    assert np.all(samples[:20 * 256] == 1000)
    assert np.all(samples[20 * 256:80 * 256] == 0)
    assert np.all(samples[80 * 256:] == 1000)


def test_decoder_overlap_has_exact_duration_and_no_hard_sample_jump():
    content = vv._crossfade_frame_wavs(wav(5, 1000), wav(5, -1000), 5, 5, 2)
    with wave.open(io.BytesIO(content)) as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), "<i2")
    assert len(samples) == 8 * 256
    assert np.all(samples[:3 * 256] == 1000) and np.all(samples[5 * 256:] == -1000)
    assert np.max(np.abs(np.diff(samples.astype(np.int32)))) <= 4


@pytest.mark.parametrize("status", [400, 422, 500])
def test_decoder_recovery_terminates_and_keeps_non_recoverable_errors(monkeypatch, status):
    sizes = []
    def post(*args, json, **kwargs):
        sizes.append(len(json["f0"]))
        return SimpleNamespace(status_code=status, text="failed")
    monkeypatch.setattr(vv.requests, "post", post)
    with pytest.raises(vv.VoicevoxScoreError) as error:
        vv._synthesize_frame_query("http://test", 6000, {
            "f0": [220.] * 8, "volume": [.5] * 8,
            "phonemes": [{"phoneme": "a", "frame_length": 8}],
        })
    assert error.value.status_code == status
    assert len(sizes) <= 4
    assert sizes[-1] == (1 if status == 500 else 8)


def test_partial_decode_retries_connections_and_preserves_score_fallback(monkeypatch):
    data = score((None, 2), (60, 70), (None, 28))
    failures = [vv.requests.ConnectionError("restart"),
                vv.VoicevoxScoreError("sing_frame_audio_query", 500, "bad")]

    def query(*args):
        raise failures.pop(0)

    monkeypatch.setattr(vv, "_query_score", query)
    monkeypatch.setattr(vv, "_wait_for_engine", lambda url: True)
    monkeypatch.setattr(vv, "_synthesize_chunk_with_score_fallback", lambda *args: wav(100))
    result = vv.synthesize_partial_score(
        data, engine_url="http://test", style_id=6000, skip_frames=[(20, 80)],
    )
    assert result == wav(100)
    assert not failures


def test_partial_decode_rejects_bad_audio_length(monkeypatch):
    data = score((None, 2), (60, 70), (None, 28))
    monkeypatch.setattr(vv, "_query_score", lambda *args: {
        "f0": [0] * 100, "volume": [0.5] * 100,
        "phonemes": [{"phoneme": "a", "frame_length": 100}],
    })
    monkeypatch.setattr(vv.requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=200, content=wav(3),
    ))
    with pytest.raises(RuntimeError, match="許容誤差"):
        vv.synthesize_partial_score(
            data, engine_url="http://test", style_id=6000, skip_frames=[(20, 80)],
        )


def test_skipped_waveform_is_fully_owned_by_pretty_and_context_sets_global_gain():
    data = score((None, 20), (60, 80), (None, 12), (82, 100), (None, 12), (65, 80), (None, 20))
    plan = hybrid.plan_phrases(data)
    length = round(plan["total_frames"] / vv.FRAME_RATE * hybrid.SAMPLE_RATE)
    baseline = np.full(length, .2, np.float32)
    pretty = np.full(length, .4, np.float32)
    omitted = np.zeros(length, bool)
    for a, b in plan["skip_frames"]:
        omitted[round(a / vv.FRAME_RATE * hybrid.SAMPLE_RATE):
                round(b / vv.FRAME_RATE * hybrid.SAMPLE_RATE)] = True
    baseline[omitted] = 0
    output, gain = hybrid.blend_audio(baseline, pretty, data, plan)
    assert gain == pytest.approx(.5)
    # Both engines match .2; any uncovered skipped sample produces a dip.
    assert np.allclose(output, .2)


def test_all_selected_uses_unity_gain_and_prevents_clipping():
    data = score((None, 2), (82, 100), (None, 20))
    plan = hybrid.plan_phrases(data)
    length = round(plan["total_frames"] / vv.FRAME_RATE * hybrid.SAMPLE_RATE)
    output, gain = hybrid.blend_audio(np.zeros(length), np.ones(length) * 1.2, data, plan)
    assert gain == 1
    assert np.max(output) == pytest.approx(.98)


def test_partial_synthesis_honors_cancellation(monkeypatch):
    monkeypatch.setattr(vv, "_partial_chunk", lambda *args: pytest.fail("must not synthesize"))
    runproc.set_cancel_check(lambda: True)
    try:
        with pytest.raises(runproc.Cancelled):
            vv.synthesize_partial_score(
                score((None, 2), (60, 70)), engine_url="http://test", style_id=6000,
                skip_frames=[],
            )
    finally:
        runproc.set_cancel_check(None)
