"""WAV由来のF0特徴だけで、低信頼なモーラ音高を保守的に補正する。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, Protocol

import numpy as np


class FrameTrack(Protocol):
    times: np.ndarray
    midi: np.ndarray
    voiced_probability: np.ndarray | None


@dataclass(frozen=True)
class DurationGate:
    threshold: float
    margin: float


@dataclass(frozen=True)
class CorrectorModel:
    candidate_deltas: np.ndarray
    duration_gates: dict[str, DurationGate]
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    coefficient: np.ndarray
    intercept: float


@lru_cache(maxsize=1)
def _model() -> CorrectorModel:
    resource = files("soramimic_video").joinpath("data/pitch_corrector_v1.json")
    payload: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    feature_count = int(payload["feature_count"])
    arrays = {}
    for key in ("scaler_mean", "scaler_scale", "coefficient"):
        values = np.asarray(payload[key], dtype=np.float64)
        if values.shape != (feature_count,):
            raise ValueError(f"invalid pitch corrector {key} shape: {values.shape}")
        arrays[key] = values
    return CorrectorModel(
        candidate_deltas=np.asarray(payload["candidate_deltas"], dtype=np.int16),
        duration_gates={
            label: DurationGate(
                threshold=float(gate["threshold"]), margin=float(gate["margin"])
            )
            for label, gate in payload["duration_gates"].items()
        },
        scaler_mean=arrays["scaler_mean"],
        scaler_scale=arrays["scaler_scale"],
        coefficient=arrays["coefficient"],
        intercept=float(payload["intercept"]),
    )


def _regions(start: float, end: float) -> tuple[tuple[float, float], ...]:
    duration = max(0.01, end - start)
    onset = start + min(0.10, 0.30 * duration)
    core_end = end - min(0.04, 0.10 * duration)
    if core_end <= onset:
        onset, core_end = start, end
    return (
        (start, end),
        (start + min(0.10, 0.30 * duration), end),
        (onset, core_end),
        (start - 0.05, end + 0.05),
        (start, start + 0.45 * duration),
        (start + 0.55 * duration, end),
    )


def _region_features(
    track: FrameTrack,
    candidates: np.ndarray,
    base: int,
    start: float,
    end: float,
) -> np.ndarray:
    if track.voiced_probability is None:
        raise ValueError("voiced probability is required for pitch correction")
    probability = np.nan_to_num(track.voiced_probability, nan=0.0)
    in_region = (track.times >= start) & (track.times < end)
    valid = in_region & np.isfinite(track.midi)
    total = int(np.sum(in_region))
    if not np.any(valid):
        empty = np.zeros((len(candidates), 21), dtype=np.float32)
        empty[:, 5:9] = 12.0
        return empty
    values = track.midi[valid]
    weights = np.maximum(probability[valid], 0.01)
    weight_sum = float(np.sum(weights)) + 1e-9
    rounded = np.rint(values).astype(int)
    soft20 = np.asarray(
        [
            np.sum(weights * np.exp(-0.5 * ((values - pitch) / 0.20) ** 2))
            / weight_sum
            for pitch in candidates
        ]
    )
    soft35 = np.asarray(
        [
            np.sum(weights * np.exp(-0.5 * ((values - pitch) / 0.35) ** 2))
            / weight_sum
            for pitch in candidates
        ]
    )
    soft70 = np.asarray(
        [
            np.sum(weights * np.exp(-0.5 * ((values - pitch) / 0.70) ** 2))
            / weight_sum
            for pitch in candidates
        ]
    )
    hard_weighted = np.asarray(
        [np.sum(weights[rounded == pitch]) / weight_sum for pitch in candidates]
    )
    hard_plain = np.asarray([np.mean(rounded == pitch) for pitch in candidates])
    median = float(np.median(values))
    weighted_mean = float(np.sum(values * weights) / weight_sum)
    q10, q90 = np.quantile(values, (0.10, 0.90))
    base_soft = soft35[int(np.where(candidates == base)[0][0])]
    rank = np.argsort(np.argsort(-soft35)).astype(float) / max(1, len(candidates) - 1)
    finite_fraction = len(values) / max(1, total)
    mean_probability = float(np.mean(probability[in_region])) if total else 0.0
    spread = float(np.std(values))
    iqr = float(np.subtract(*np.quantile(values, (0.75, 0.25))))
    global_features = np.tile(
        [
            finite_fraction,
            mean_probability,
            spread,
            iqr,
            len(values) / 100.0,
            float(np.max(soft35)),
            float(np.max(hard_weighted)),
        ],
        (len(candidates), 1),
    )
    return np.column_stack(
        [
            soft20,
            soft35,
            soft70,
            hard_weighted,
            hard_plain,
            np.clip(candidates - median, -12, 12),
            np.clip(np.abs(candidates - median), 0, 12),
            np.clip(candidates - weighted_mean, -12, 12),
            np.clip(np.abs(candidates - weighted_mean), 0, 12),
            (candidates >= q10) & (candidates <= q90),
            rank,
            soft35 - base_soft,
            candidates == np.rint(median),
            candidates == np.rint(weighted_mean),
            global_features,
        ]
    ).astype(np.float32)


def _note_features(
    tracks: tuple[FrameTrack, FrameTrack],
    start: float,
    end: float,
    base: int,
    previous: int,
    following: int,
    deltas: np.ndarray,
) -> np.ndarray:
    candidates = base + deltas
    acoustic = []
    for track in tracks:
        for region_start, region_end in _regions(start, end):
            acoustic.append(
                _region_features(track, candidates, base, region_start, region_end)
            )
    duration = max(0.01, end - start)
    global_features = np.column_stack(
        [
            np.full(len(candidates), duration),
            np.full(len(candidates), math.log(duration)),
            np.full(len(candidates), base / 80.0),
            candidates / 80.0,
            deltas / 12.0,
            np.abs(deltas) / 12.0,
            (deltas / 12.0) ** 2,
            (candidates - previous) / 12.0,
            (following - candidates) / 12.0,
            np.full(len(candidates), (base - previous) / 12.0),
            np.full(len(candidates), (following - base) / 12.0),
            candidates == previous,
            candidates == following,
            np.sin(2 * np.pi * (candidates % 12) / 12),
            np.cos(2 * np.pi * (candidates % 12) / 12),
        ]
    ).astype(np.float32)
    return np.column_stack([global_features, *acoustic]).astype(np.float32)


def _duration_gate(duration: float, model: CorrectorModel) -> DurationGate | None:
    if duration < 0.1:
        return None
    if duration < 0.2:
        label = "100to200ms"
    elif duration < 0.4:
        label = "200to400ms"
    else:
        label = "ge400ms"
    return model.duration_gates.get(label)


def _candidate_probabilities(features: np.ndarray, model: CorrectorModel) -> np.ndarray:
    standardized = (features.astype(np.float64) - model.scaler_mean) / model.scaler_scale
    logits = standardized @ model.coefficient + model.intercept
    return np.exp(-np.logaddexp(0.0, -logits))


def correct_midi_notes(
    primary: FrameTrack,
    short_window: FrameTrack,
    spans: list[tuple[float, float]],
    baseline: list[int],
) -> list[int]:
    """PJSで学習した補正器を高確度の候補だけに適用する。"""
    if primary.voiced_probability is None or short_window.voiced_probability is None:
        return baseline
    if len(spans) != len(baseline):
        raise ValueError("spans and baseline must have the same length")
    model = _model()
    deltas = model.candidate_deltas
    base_index = int(np.where(deltas == 0)[0][0])
    corrected = baseline.copy()
    for index, ((start, end), base) in enumerate(zip(spans, baseline, strict=True)):
        gate = _duration_gate(end - start, model)
        if gate is None:
            continue
        previous = baseline[index - 1] if index else base
        following = baseline[index + 1] if index + 1 < len(baseline) else base
        features = _note_features(
            (primary, short_window),
            start,
            end,
            base,
            previous,
            following,
            deltas,
        )
        probabilities = _candidate_probabilities(features, model)
        best = int(np.argmax(probabilities))
        if (
            best != base_index
            and probabilities[best] >= gate.threshold
            and probabilities[best] >= probabilities[base_index] + gate.margin
        ):
            corrected[index] = base + int(deltas[best])
    return corrected
