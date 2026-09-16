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
from typing import Any

import jaconv

from .kana import normalize_long_vowels

logger = logging.getLogger(__name__)

KANA_WHISPER_MODEL = "sbintuitions/kana-whisper"
KANA_WHISPER_REVISION = "88ecb3d79c5846cb4fcf76f4107b84c8fa2acd82"
KANA_CONTEXT_PADDING_SEC = 1.5
KANA_CONTEXT_MAX_LINE_SPAN_SEC = 9.0
KANA_CONTEXT_MAX_SEC = 24.0
KANA_MAX_WINDOWS = 256
_KATAKANA_RE = re.compile(r"[ァ-ヶー]+")
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


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
    distances: tuple[tuple[int, ...], ...]
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
    return normalize_long_vowels(kana.replace("ヲ", "オ"))


def _candidate_key(reading: str) -> str:
    return normalize_long_vowels(jaconv.hira2kata(reading).replace("ヲ", "オ"))


def _substring_distance(needle: str, haystack: str) -> int:
    """Levenshtein distance to the best substring of ``haystack``."""
    if not needle:
        return 0
    if not haystack:
        return len(needle)
    previous = [0] * (len(haystack) + 1)
    for row, left in enumerate(needle, 1):
        current = [row]
        for column, right in enumerate(haystack, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left != right),
                )
            )
        previous = current
    return min(previous)


def choose_reading(candidates: Sequence[str], evidence: Sequence[str]) -> ReadingDecision:
    """Conservatively rerank bounded candidates using mix/vocal kana evidence.

    A non-default candidate must beat the default in at least one reliable view,
    lose in none, and clear conservative acoustic and dictionary-order priors. This
    keeps raw ASR mistakes from becoming lyric edits. Distances are normalized by
    candidate length so shorter dictionary readings do not win merely because they
    have fewer characters that can differ.
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

    distances = tuple(
        tuple(_substring_distance(key, transcript) for transcript in normalized) for key in keys
    )
    normalized_distances = tuple(
        tuple(distance / max(1, len(key)) for distance in row)
        for key, row in zip(keys, distances, strict=True)
    )
    totals = {index: sum(normalized_distances[index]) for index in eligible}
    best_total = min(totals.values())
    best_index = next(index for index in eligible if totals[index] == best_total)
    # Candidate order is a linguistic prior (yomi, then UniDic paths).  When two
    # pronunciations are within one edit across both acoustic views, keep the
    # earlier dictionary path instead of overfitting a KanaWhisper consonant error.
    tie_margin = 1 / len(keys[best_index])
    selected = next(
        index for index in eligible if totals[index] <= best_total + tie_margin
    )
    if selected == 0:
        return ReadingDecision(
            0, "default-or-tie", normalized, distances, normalized_distances
        )
    supports = 0
    for view in range(len(normalized)):
        default_distance = normalized_distances[0][view]
        selected_distance = normalized_distances[selected][view]
        reliable = (
            default_distance <= max(0.3, 2 / len(keys[0]))
            or selected_distance <= max(0.3, 2 / len(keys[selected]))
        )
        if not reliable:
            continue
        if selected_distance > default_distance:
            return ReadingDecision(
                0, "conflicting-evidence", normalized, distances, normalized_distances
            )
        if selected_distance < default_distance:
            supports += 1
    if supports == 0:
        return ReadingDecision(
            0, "insufficient-evidence", normalized, distances, normalized_distances
        )
    total_gain = totals[0] - totals[selected]
    weak_gain = 3 / max(len(keys[0]), len(keys[selected]))
    if total_gain < weak_gain and 0.0 not in normalized_distances[selected]:
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
