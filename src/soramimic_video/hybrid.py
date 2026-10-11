"""波音リツの音域外フレーズをPrettyPitchで補う歌唱合成。"""
from __future__ import annotations

import io
import json
import math
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from . import prettypitch, runproc
from . import voicevox as vv
from .octave import resolve_auto_shift
from .project import Project

PHRASE_REST_SEC = 0.12
REST_MARGIN_SEC = 0.15
PRETTY_CONTEXT_SEC = 0.6
CROSSFADE_SEC = 0.02
VOICEVOX_CONTEXT_SEC = 0.08
SAMPLE_RATE = 44100


def merge_intervals(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1] + 1e-8:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def plan_phrases(score: dict[str, Any]) -> dict[str, Any]:
    """One frame-grid score owns lyrics, pitch and timing for both engines."""
    notes = []
    cursor = 0
    for note in score["notes"]:
        end = cursor + note["frame_length"]
        notes.append(dict(note, start=cursor / vv.FRAME_RATE, end=end / vv.FRAME_RATE))
        cursor = end
    phrases: list[list[int]] = []
    current: list[int] = []
    for i, note in enumerate(notes):
        if note["key"] is None:
            if note["end"] - note["start"] >= PHRASE_REST_SEC and current:
                phrases.append(current)
                current = []
        else:
            current.append(i)
    if current:
        phrases.append(current)
    selected = [
        phrase for phrase in phrases
        if any(not vv.SAFE_KEY_MIN <= notes[i]["key"] <= vv.SAFE_KEY_MAX for i in phrase)
    ]
    spans = []
    contexts = []
    for phrase in selected:
        first, last = phrase[0], phrase[-1]
        start, end = notes[first]["start"], notes[last]["end"]
        contexts.append((max(0.0, start - PRETTY_CONTEXT_SEC), end + PRETTY_CONTEXT_SEC))
        if first and notes[first - 1]["key"] is None:
            start -= min(REST_MARGIN_SEC, notes[first - 1]["frame_length"] / vv.FRAME_RATE / 2)
        if last + 1 < len(notes) and notes[last + 1]["key"] is None:
            end += min(REST_MARGIN_SEC, notes[last + 1]["frame_length"] / vv.FRAME_RATE / 2)
        spans.append((start, end))
    spans = merge_intervals(spans)
    contexts = merge_intervals(contexts)
    kept = [
        i for i, note in enumerate(notes) if note["key"] is not None
        and any(note["start"] < end and note["end"] > start for start, end in contexts)
    ]
    margin = CROSSFADE_SEC / 2 + VOICEVOX_CONTEXT_SEC
    skipped = [
        (math.ceil((start + margin) * vv.FRAME_RATE), math.floor((end - margin) * vv.FRAME_RATE))
        for start, end in spans if end - start > 2 * margin
    ]
    return {"intervals": spans, "context_notes": kept, "skip_frames": skipped,
            "total_frames": cursor, "selected_phrases": len(selected)}


def build_partial_ust(score: dict[str, Any], kept: Sequence[int]) -> str:
    """Keep entire context notes and exact fractional UST ticks, without shifting time."""
    chosen = set(kept)
    items: list[dict[str, Any]] = []
    for i, note in enumerate(score["notes"]):
        item = dict(note) if i in chosen else dict(note, key=None, lyric="R")
        if items and item["key"] is None and items[-1]["key"] is None:
            items[-1]["frame_length"] += item["frame_length"]
        else:
            items.append(item)
    sections = ["[#VERSION]\nUST Version1.2\n[#SETTING]\nTempo=120\n"]
    for i, note in enumerate(items):
        sections.append(
            f"[#{i:04d}]\nLength={note['frame_length'] * 960 / vv.FRAME_RATE:.8f}\n"
            f"Lyric={note['lyric']}\nNoteNum={note['key'] if note['key'] is not None else 60}\n"
        )
    return "".join(sections) + "[#TRACKEND]\n"


def _read_pcm(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise RuntimeError("ハイブリッド合成にはモノラル16-bit WAVが必要です")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(
            np.float32
        ) / 32768, source.getframerate()


def blend_audio(
    baseline: np.ndarray, pretty: np.ndarray, score: dict[str, Any], plan: dict[str, Any],
    sample_rate: int = SAMPLE_RATE,
) -> tuple[np.ndarray, float]:
    if len(baseline) != len(pretty) or not (
        np.all(np.isfinite(baseline)) and np.all(np.isfinite(pretty))
    ):
        raise RuntimeError("合成音声の長さまたはサンプル値が不正です")
    length = len(baseline)
    mask = np.zeros(length, dtype=np.float32)
    half = CROSSFADE_SEC / 2
    for start, end in plan["intervals"]:
        a, b = max(0, round((start - half) * sample_rate)), min(
            length, round((end + half) * sample_rate)
        )
        times = np.arange(a, b) / sample_rate
        ramp = np.minimum((times - start + half) / CROSSFADE_SEC,
                          (end + half - times) / CROSSFADE_SEC)
        mask[a:b] = np.maximum(mask[a:b], np.clip(ramp, 0, 1))
    # Compare only the same sung context, away from phoneme and decoder edges.
    # If the selected phrases cover everything, unity gain needs no extra synthesis.
    calibration = np.zeros(length, dtype=bool)
    cursor = 0
    for i, note in enumerate(score["notes"]):
        end = cursor + note["frame_length"]
        if i in plan["context_notes"]:
            a = max(0, round((cursor / vv.FRAME_RATE + 0.03) * sample_rate))
            b = min(length, round((end / vv.FRAME_RATE - 0.03) * sample_rate))
            if b > a:
                calibration[a:b] = True
        cursor = end
    for start, end in plan["intervals"]:
        a = max(0, round((start - half - VOICEVOX_CONTEXT_SEC) * sample_rate))
        b = min(length, round((end + half + VOICEVOX_CONTEXT_SEC) * sample_rate))
        calibration[a:b] = False
    gain = 1.0
    if np.count_nonzero(calibration) >= sample_rate * 0.1:
        vv_rms = float(np.sqrt(np.mean(baseline[calibration] ** 2)))
        pp_rms = float(np.sqrt(np.mean(pretty[calibration] ** 2)))
        if min(vv_rms, pp_rms) > 0.001:
            gain = float(np.clip(vv_rms / pp_rms, 0.5, 2.0))
    output = baseline * (1 - mask) + pretty * gain * mask
    peak = float(np.max(np.abs(output), initial=0))
    if peak > 0.98:
        output *= 0.98 / peak
    return output, gain


def run_hybrid(
    project: Project, project_dir: Path, *, engine_url: str = vv.DEFAULT_ENGINE_URL,
    transpose: int = 0, auto_octave: bool = True, octave_keys: list[int] | None = None,
    threads: int = 4, dry_run: bool = False,
    progress_cb: Callable[[float], None] | None = None,
) -> Path | None:
    from .synthesize import vocal_path

    if auto_octave:
        transpose += resolve_auto_shift(
            project,
            octave_keys if octave_keys is not None else [n.midi_note for n in project.notes],
            transpose, vv.SAFE_KEY_MIN, vv.SAFE_KEY_MAX, "VOICEVOX + PrettyPitch",
        )
    else:
        project.song.key_shift = 0
    score = vv.build_score(project, transpose=transpose)
    plan = plan_phrases(score)
    work = Path(project_dir).resolve() / "hybrid"
    work.mkdir(parents=True, exist_ok=True)
    (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if dry_run:
        return None
    if plan["intervals"]:
        if error := prettypitch.installation_error():
            raise RuntimeError(error)
    if progress_cb:
        progress_cb(0.0)
    content = vv.synthesize_partial_score(
        score, engine_url=engine_url, style_id=vv.SING_TEACHER_ID,
        skip_frames=plan["skip_frames"],
        progress_cb=(lambda fraction: progress_cb(fraction * 0.45)) if progress_cb else None,
    )
    output = vocal_path(project_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not plan["intervals"]:
        output.write_bytes(content)
    else:
        runproc.raise_if_cancelled()
        vv_file = work / "voicevox.wav"
        vv_file.write_bytes(content)
        converted = work / "voicevox-44100.wav"
        runproc.run([
            "ffmpeg", "-y", "-v", "error", "-i", str(vv_file), "-ar", str(SAMPLE_RATE),
            "-ac", "1", "-c:a", "pcm_s16le", str(converted),
        ], check=True, capture_output=True)
        baseline, rate = _read_pcm(converted)
        pretty_path = prettypitch.run_partial_score(
            build_partial_ust(score, plan["context_notes"]), work,
            duration=plan["total_frames"] / vv.FRAME_RATE, threads=threads,
        )
        pretty = np.load(pretty_path, allow_pickle=False)
        if (rate != SAMPLE_RATE or pretty.ndim != 1
                or abs(len(baseline) - len(pretty)) > 1):
            raise RuntimeError("VOICEVOXとPrettyPitchの音声時刻が一致しません")
        baseline = np.pad(baseline, (0, max(0, len(pretty) - len(baseline))))[:len(pretty)]
        mixed, gain = blend_audio(baseline, pretty, score, plan)
        plan["prettypitch_gain"] = gain
        (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes((np.clip(mixed, -1, 1) * 32767).astype("<i2").tobytes())
        output.write_bytes(buffer.getvalue())
    if progress_cb:
        progress_cb(1.0)
    return output
