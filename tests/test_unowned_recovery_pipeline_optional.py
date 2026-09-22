"""Focused integration coverage for lyric-free Stage 3 note recovery."""

import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("wav_to_xf.pipeline")


@pytest.mark.parametrize(
    ("retry_texts", "expected_text", "selected_source"),
    [
        (("ララララ", "ララララ"), "ララララ", "exact"),
        (("ダラダラ", "ナラナラ"), "アアアア", "vowel-continuation"),
    ],
)
def test_unowned_note_run_retries_deterministically_and_realigns(
    monkeypatch, tmp_path, retry_texts, expected_text, selected_source,
):
    import soramimic_video.stage3 as stage3
    from soramimic_video import (
        analyze_audio as analyze_audio_module,
    )
    from soramimic_video import (
        audio_melody,
        mora_align,
        reading,
        transcribe,
        vocal_activity,
    )
    from soramimic_video.audio_melody import MelodyNote
    from soramimic_video.mora_align import AlignedMora
    from soramimic_video.transcribe import TranscribedLine
    from soramimic_video.vocal_activity import (
        VocalActivityLine,
        VocalActivityProfile,
    )

    audio = tmp_path / "input.wav"
    vocals = tmp_path / "vocals.wav"
    initial = TranscribedLine(0.0, 1.0, "歌")
    notes = [MelodyNote(0.0, 1.0, 60)] + [
        MelodyNote(2.0 + index * 0.45, 2.3 + index * 0.45, 62 + index % 2)
        for index in range(10)
    ]
    monkeypatch.setenv("SORAMIMIC_AUDIO_INFERENCE_URL", "http://127.0.0.1:8320")
    monkeypatch.setattr(analyze_audio_module, "_require_audio_pipeline", lambda: None)
    monkeypatch.setattr(
        analyze_audio_module,
        "_run_audio_models",
        lambda *_args, **_kwargs: (vocals, None, [initial], notes),
    )
    monkeypatch.setattr(
        audio_melody, "configured_capabilities", lambda: {"sheetsage2": True}
    )
    monkeypatch.setattr(
        vocal_activity,
        "measure_vocal_activity",
        lambda _path, windows: VocalActivityProfile(
            -12.0,
            tuple(VocalActivityLine(-15.0, -3.0, 0.9, True) for _ in windows),
        ),
    )
    monkeypatch.setattr(
        reading,
        "reading_candidates",
        lambda text: {
            "歌": ["ウタ"],
            "ララララ": ["ララララ"],
            "ダラダラ": ["ダラダラ"],
            "ナラナラ": ["ナラナラ"],
            "アアアア": ["アアアア"],
        }[text],
    )
    emissions = object()
    monkeypatch.setattr(mora_align, "compute_emissions", lambda *_args: emissions)

    def align(_path, variants, **kwargs):
        aligned = []
        for line_index, (choices, window) in enumerate(
            zip(variants, kwargs["line_windows"], strict=True)
        ):
            moras = choices[0]
            score = 0.0001 if "".join(moras) in {"ダラダラ", "ナラナラ"} else 0.01
            for mora_index, mora in enumerate(moras):
                width = (window[1] - window[0]) / len(moras)
                aligned.append(AlignedMora(
                    line_index,
                    mora_index,
                    mora,
                    window[0] + mora_index * width,
                    window[0] + (mora_index + 1) * width,
                    score,
                ))
        return aligned, [0] * len(variants)

    monkeypatch.setattr(mora_align, "align_moras_with_variants", align)
    retries = []

    def retry(_path, start, end, _model, _device, **kwargs):
        retries.append((start, end, kwargs))
        return [TranscribedLine(start, end, retry_texts[len(retries) - 1])]

    monkeypatch.setattr(transcribe, "transcribe_window", retry)
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(info=lambda _path: SimpleNamespace(duration=7.0)),
    )

    note_rows = [
        {"id": f"sheetsage-{index + 1}", "start_sec": note.start_sec,
         "end_sec": note.end_sec}
        for index, note in enumerate(notes[1:])
    ]
    link_rows = [
        {
            "operation": "note_only",
            "singing_unit_ids": [],
            "note_candidate_ids": [row["id"]],
        }
        for row in note_rows
    ]
    calls = 0

    class StopAfterFinalStage3(Exception):
        pass

    def build(line_texts, selected_readings, aligned, _notes, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(to_json=lambda: json.dumps({
                "note_candidates": note_rows,
                "links": link_rows,
            })), object()
        assert line_texts == ["歌", expected_text]
        assert selected_readings == ["ウタ", expected_text]
        assert "".join(item.kana for item in aligned if item.line == 1) == expected_text
        raise StopAfterFinalStage3

    monkeypatch.setattr(stage3, "build_stage3_layers", build)

    with pytest.raises(StopAfterFinalStage3):
        analyze_audio_module.analyze_audio(audio, tmp_path / "project", device="cpu")

    assert len(retries) == 2
    assert all(options == {"temperature": 0.0} for _start, _end, options in retries)
    recognition = json.loads(
        (tmp_path / "project/analyze_audio/recognition.json").read_text()
    )
    recovery = recognition["semantic_gate"]["unowned_note_recoveries"][0]
    assert recovery["status"] == "accepted"
    assert recovery["note_count"] == 10
    assert recovery["temperature"] == 0.0
    assert recovery["selected_source"] == selected_source
    assert recognition["segments"][-1]["surface"] == expected_text
