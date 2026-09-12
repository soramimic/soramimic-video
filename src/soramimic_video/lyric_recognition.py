"""Optional multi-view lyric recognition using the installed wav_to_xf contract."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from . import runproc
from .mora_align import CTCEmissions, compute_emissions, decode_kana_window
from .reading import reading_candidates
from .transcribe import DEFAULT_WHISPER_MODEL


def require_lyric_pipeline() -> None:
    try:
        from wav_to_xf.cplus import from_cplus_assignments  # noqa: F401
        from wav_to_xf.realization import compile_realization  # noqa: F401
        from wav_to_xf.recognition import recognize_unknown_lyrics  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "evidence歌詞パイプラインにはwav-to-xfパッケージが必要です。"
            "利用可能なローカルチェックアウトを uv pip install <checkout> で追加してください。"
        ) from exc


def transcribe_multiview(
    vocals_path: Path,
    mix_path: Path,
    *,
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str | None = None,
    emissions: CTCEmissions | None = None,
) -> tuple[Any, CTCEmissions]:
    """Keep all Whisper views and score readings against independent kana CTC.

    One model instance and one vocal CTC matrix serve all passes. No-VAD is a
    conditional recovery pass. Segment windows are not vowel timing evidence.
    """
    require_lyric_pipeline()
    import soundfile as sf
    from faster_whisper import WhisperModel
    from wav_to_xf import Evidence, ReadingCandidate
    from wav_to_xf.recognition import (
        AcousticPronunciation,
        RecognitionBatch,
        RecognitionHypothesis,
        recognize_unknown_lyrics,
    )

    emissions = emissions or compute_emissions(vocals_path, device)
    duration_sec = float(sf.info(vocals_path).duration)
    if duration_sec <= 0:
        raise ValueError("音源が空です")
    model = WhisperModel(model_size, device=device or "auto")
    paths = {"vocals": vocals_path, "mix": mix_path}

    def produce(request: Any) -> Any:
        runproc.raise_if_cancelled()
        pass_id = f"whisper-{request.view}-{'vad' if request.vad_enabled else 'no-vad'}"
        segments, info = model.transcribe(
            str(paths[request.view]), language="ja", vad_filter=request.vad_enabled,
        )
        hypotheses, acoustic, evidence, diagnostics = [], [], [], []
        for index, segment in enumerate(segments):
            runproc.raise_if_cancelled()
            start = max(0.0, float(segment.start))
            end = min(duration_sec, float(segment.end))
            if not math.isfinite(start + end) or end <= start:
                diagnostics.append(f"invalid_segment:{pass_id}:{index}")
                continue
            hypothesis_id = f"{pass_id}:{index}"
            raw_score = float(segment.avg_logprob)
            confidence = math.exp(min(0.0, raw_score)) if math.isfinite(raw_score) else 0.0
            evidence_id = f"score:{hypothesis_id}"
            evidence.append(Evidence(evidence_id, f"faster-whisper:{model_size}",
                                     "transcript-score", confidence,
                                     {"score_kind": "exp_avg_logprob", "timing_calibrated": False}))
            readings = tuple(ReadingCandidate(kana, "dictionary-reading", 1.0)
                             for kana in reading_candidates(segment.text.strip()) if kana)
            hypotheses.append(RecognitionHypothesis(
                hypothesis_id, pass_id, f"faster-whisper:{model_size}", request.view,
                request.vad_enabled, str(index), start, end, info.language or "ja",
                confidence, 0.0, segment.text.strip(), readings,
                float(segment.no_speech_prob), (evidence_id,),
            ))
            kana, ctc_score = decode_kana_window(emissions, start, end)
            if kana:
                # Windows are segmented by time only; no hypothesis text enters
                # the decoder. Coverage means analyzed audio, not voiced time.
                acoustic.append(AcousticPronunciation(
                    f"ctc:{hypothesis_id}", "reazon-kana-ctc", "vocals", start, end,
                    ctc_score, 0.0, kana=kana, stream_id=pass_id,
                ))
        return RecognitionBatch(tuple(hypotheses), tuple(acoustic), tuple(evidence),
                                tuple(diagnostics))

    views = ("vocals",) if vocals_path.resolve() == mix_path.resolve() else ("vocals", "mix")
    result = recognize_unknown_lyrics(produce, duration_sec=duration_sec, views=views)
    return result, emissions
