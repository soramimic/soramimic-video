"""KanaWhisper evidence for conservative pronunciation selection.

The model never replaces lyric text.  It only helps choose among bounded readings
derived from the already selected surface text.  ReazonSpeech CTC remains the
source of mora timing.
"""

from __future__ import annotations

import gc
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import jaconv
from kanasim import WeightedLevenshtein, create_kana_distance_calculator

from .kana import normalize_audio_reading, normalize_long_vowels

logger = logging.getLogger(__name__)

KANA_WHISPER_MODEL = "sbintuitions/kana-whisper"
KANA_WHISPER_REVISION = "88ecb3d79c5846cb4fcf76f4107b84c8fa2acd82"
KANA_CONTEXT_PADDING_SEC = 1.5
KANA_CONTEXT_MAX_LINE_SPAN_SEC = 9.0
KANA_CONTEXT_MAX_SEC = 24.0
KANA_MAX_WINDOWS = 256
_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_KANA_DISTANCE = cast(
    WeightedLevenshtein,
    create_kana_distance_calculator(symmetric=True, normalize=True),
)
# Kanasim's normalization scales acoustic cost tables; it does not divide by
# candidate length. Retain a small dictionary-order prior for marginal evidence.
_MIN_TOTAL_DISTANCE_GAIN = 0.25


@dataclass(frozen=True)
class KanaContext:
    start_sec: float
    end_sec: float
    line_indices: tuple[int, ...]


@dataclass(frozen=True)
class ReadingDecision:
    selected_index: int
    reason: str
    normalized_evidence: tuple[str, ...]
    distances: tuple[tuple[float, ...], ...]
    normalized_distances: tuple[tuple[float, ...], ...] = ()


def model_available() -> bool:
    """Return whether the pinned model snapshot is complete in the local cache."""
    try:
        from huggingface_hub import snapshot_download

        snapshot = Path(
            snapshot_download(
                KANA_WHISPER_MODEL,
                revision=KANA_WHISPER_REVISION,
                local_files_only=True,
            )
        )
    except Exception:  # noqa: BLE001 - health checks must degrade to unavailable
        return False
    return all(
        (snapshot / name).is_file()
        for name in (
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "tokenizer.json",
        )
    )


def normalize_kana_evidence(text: str) -> str:
    """Normalize free KanaWhisper text for closed-candidate comparison."""
    kana = "".join(_KATAKANA_RE.findall(jaconv.hira2kata(text)))
    return normalize_audio_reading(normalize_long_vowels(kana.replace("ヲ", "オ")))


def _candidate_key(reading: str) -> str:
    kana = normalize_audio_reading(jaconv.hira2kata(reading).replace("ヲ", "オ"))
    return normalize_audio_reading(normalize_long_vowels(kana))


def _kanasim_moras(text: str) -> list[str]:
    # Consecutive long-vowel marks occur in expressive singing transcripts, but
    # Kanasim's mora table represents a prolonged mora with a single mark.
    return _KANA_DISTANCE.preprocess_func(re.sub("ー+", "ー", text))


def _phonetic_substring_distance(needle: str, haystack: str) -> float:
    """Return raw Kanasim distance to the best substring of ``haystack``.

    KanaWhisper evidence covers a multi-line context, so a whole-string distance
    would charge unrelated surrounding lyrics. This uses Kanasim's weighted mora
    costs with free evidence prefixes/suffixes; candidate edits keep their full
    summed costs.
    """
    left_moras = _kanasim_moras(needle)
    right_moras = _kanasim_moras(haystack)
    if not left_moras:
        return 0.0

    previous = [0.0] * (len(right_moras) + 1)
    for left in left_moras:
        delete_cost = (
            _KANA_DISTANCE.delete_cost_func(left)
            if _KANA_DISTANCE.delete_cost_func else _KANA_DISTANCE.delete_cost
        )
        current = [previous[0] + delete_cost]
        for column, right in enumerate(right_moras, 1):
            insert_cost = (
                _KANA_DISTANCE.insert_cost_func(right)
                if _KANA_DISTANCE.insert_cost_func else _KANA_DISTANCE.insert_cost
            )
            replace_cost = (
                _KANA_DISTANCE.replace_cost_func(left, right)
                if _KANA_DISTANCE.replace_cost_func else _KANA_DISTANCE.replace_cost
            )
            current.append(min(
                previous[column] + delete_cost,
                current[column - 1] + insert_cost,
                previous[column - 1] + replace_cost,
            ))
        previous = current
    return min(previous)


def choose_reading(candidates: Sequence[str], evidence: Sequence[str]) -> ReadingDecision:
    """Conservatively rerank bounded candidates using mix/vocal kana evidence.

    A non-default candidate must beat the default in at least one view, lose in
    none, and clear a conservative dictionary-order prior. This keeps raw ASR
    mistakes from becoming lyric edits. Candidates are ranked by raw summed
    Kanasim distance; candidate-length-normalized values are diagnostic only.
    """
    normalized = tuple(filter(None, (normalize_kana_evidence(text) for text in evidence)))
    if len(candidates) < 2:
        return ReadingDecision(0, "single-candidate", normalized, ())
    keys = tuple(_candidate_key(candidate) for candidate in candidates)
    eligible = []
    seen_keys: set[str] = set()
    for index, key in enumerate(keys):
        if key and key not in seen_keys:
            eligible.append(index)
            seen_keys.add(key)
    if len(eligible) < 2 or not normalized:
        reason = "no-distinct-candidates" if len(eligible) < 2 else "no-evidence"
        return ReadingDecision(0, reason, normalized, ())

    distance_rows: list[tuple[float, ...]] = []
    for index, key in enumerate(keys):
        try:
            row = tuple(
                _phonetic_substring_distance(key, transcript)
                for transcript in normalized
            )
        except ValueError as exc:
            logger.warning(
                "KanaWhisper距離表にない読み候補を除外: index=%d reading=%r: %s",
                index,
                candidates[index],
                exc,
            )
            row = ()
        distance_rows.append(row)
    distances = tuple(distance_rows)
    supported = [index for index in eligible if len(distances[index]) == len(normalized)]
    normalized_distances = tuple(
        tuple(distance / max(1, len(_kanasim_moras(key))) for distance in row)
        for key, row in zip(keys, distances, strict=True)
    )
    if not supported:
        return ReadingDecision(
            0, "unsupported-kana", normalized, distances, normalized_distances
        )
    if 0 not in supported:
        return ReadingDecision(
            supported[0], "unsupported-default", normalized, distances,
            normalized_distances,
        )
    if len(supported) < 2:
        return ReadingDecision(
            0, "no-supported-alternative", normalized, distances,
            normalized_distances,
        )

    totals = {index: sum(distances[index]) for index in supported}
    best_total = min(totals.values())
    selected = next(
        index for index in supported
        if math.isclose(totals[index], best_total, abs_tol=1e-12)
    )
    if selected == 0:
        return ReadingDecision(
            0, "default-or-tie", normalized, distances, normalized_distances
        )
    supports = 0
    for view in range(len(normalized)):
        default_distance = distances[0][view]
        selected_distance = distances[selected][view]
        if selected_distance > default_distance and not math.isclose(
            selected_distance, default_distance, abs_tol=1e-12
        ):
            return ReadingDecision(
                0, "conflicting-evidence", normalized, distances, normalized_distances
            )
        if selected_distance < default_distance and not math.isclose(
            selected_distance, default_distance, abs_tol=1e-12
        ):
            supports += 1
    if supports == 0:
        return ReadingDecision(
            0, "insufficient-evidence", normalized, distances, normalized_distances
        )
    total_gain = totals[0] - totals[selected]
    if total_gain < _MIN_TOTAL_DISTANCE_GAIN and 0.0 not in distances[selected]:
        return ReadingDecision(
            0, "weak-evidence", normalized, distances, normalized_distances
        )
    return ReadingDecision(
        selected, "kana-evidence", normalized, distances, normalized_distances
    )


def build_kana_contexts(
    line_windows: Sequence[tuple[float, float]],
    *,
    audio_duration: float,
) -> tuple[list[KanaContext], list[int | None]]:
    """Pack nearby lyric lines into bounded context windows for KanaWhisper."""
    contexts: list[KanaContext] = []
    assignments: list[int | None] = [None] * len(line_windows)
    group: list[int] = []

    def publish(indices: list[int]) -> None:
        if not indices:
            return
        first = line_windows[indices[0]][0]
        last = line_windows[indices[-1]][1]
        start = max(0.0, first - KANA_CONTEXT_PADDING_SEC)
        end = min(audio_duration, last + KANA_CONTEXT_PADDING_SEC)
        if end <= start or end - start > KANA_CONTEXT_MAX_SEC:
            return
        context_index = len(contexts)
        contexts.append(KanaContext(start, end, tuple(indices)))
        for index in indices:
            assignments[index] = context_index

    for index, (start, end) in enumerate(line_windows):
        if (
            not math.isfinite(start + end)
            or start < 0
            or end <= start
            or end - start > KANA_CONTEXT_MAX_SEC - 2 * KANA_CONTEXT_PADDING_SEC
        ):
            publish(group)
            group = []
            continue
        if group and end - line_windows[group[0]][0] > KANA_CONTEXT_MAX_LINE_SPAN_SEC:
            publish(group)
            group = []
        group.append(index)
    publish(group)
    if len(contexts) > KANA_MAX_WINDOWS:
        raise ValueError(f"KanaWhisper context count exceeds {KANA_MAX_WINDOWS}")
    return contexts, assignments


def transcribe_kana_windows(
    audio_path: Path,
    windows: Sequence[tuple[float, float]],
    device: str = "auto",
) -> list[str]:
    """Transcribe bounded windows through the shared service or locally."""
    from .audio_inference import configured_url, transcribe_kana_windows_remote

    if configured_url() is not None:
        return transcribe_kana_windows_remote(audio_path, windows, device)
    return _transcribe_kana_windows_local(audio_path, windows, device)


def _load_model(device: str) -> Any:
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    actual_device = "cuda" if device == "auto" and torch.cuda.is_available() else device
    if actual_device == "auto":
        actual_device = "cpu"
    dtype = torch.float16 if actual_device.startswith("cuda") else torch.float32
    key = (actual_device, str(dtype))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        KANA_WHISPER_MODEL,
        revision=KANA_WHISPER_REVISION,
        torch_dtype=dtype,
        use_safetensors=True,
    ).to(actual_device)
    processor = AutoProcessor.from_pretrained(
        KANA_WHISPER_MODEL,
        revision=KANA_WHISPER_REVISION,
    )
    transcriber = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=dtype,
        device=actual_device,
    )
    _MODEL_CACHE[key] = transcriber
    return transcriber


def _transcribe_kana_windows_local(
    audio_path: Path,
    windows: Sequence[tuple[float, float]],
    device: str = "auto",
    *,
    cancel_check: Callable[[], Any] | None = None,
) -> list[str]:
    """Run KanaWhisper for already validated song-clock windows."""
    try:
        import librosa
    except ImportError as exc:
        raise RuntimeError("KanaWhisper用の依存がありません(uv sync --extra audio)") from exc
    if len(windows) > KANA_MAX_WINDOWS:
        raise ValueError(f"KanaWhisper window count exceeds {KANA_MAX_WINDOWS}")
    waveform, sample_rate = librosa.load(str(audio_path), sr=16000, mono=True)
    transcriber = _load_model(device)
    results: list[str] = []
    for start, end in windows:
        if cancel_check is not None:
            cancel_check()
        first = max(0, round(start * sample_rate))
        last = min(len(waveform), round(end * sample_rate))
        if first >= last:
            raise ValueError("KanaWhisper window is outside the input audio")
        output = transcriber(
            waveform[first:last],
            generate_kwargs={"language": "ja", "task": "transcribe"},
        )
        results.append(str(output["text"]).strip())
    return results


def release_kana_whisper_cache() -> None:
    """Release cached model memory for tests and controlled service maintenance."""
    _MODEL_CACHE.clear()
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass
