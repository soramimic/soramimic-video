"""analyze-audio ステージ: 歌唱音源 → project.json。

XF MIDI の代わりに歌唱音源(wav/mp3)を入力の起点にする(issue #1)。

1. demucs でボーカル/伴奏に分離(伴奏は mix ステージでそのまま使う)
2. 正式歌詞があれば、その文字列を一切書き換えず forced alignment する
3. KanaWhisper は表層を変えず、yomi / UniDic N-best の読み候補だけを選ぶ
4. Reazon kana CTC は選択済みの読みを変えず、モーラ時刻だけを整列する
5. SheetSage2 の原音mixノート候補を Stage 3 でモーラへ対応づける
6. 固定BPMの tick に換算して project.json を組み立てる

目視検証用に moras.srt / lines.srt も書き出す。
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .audio_project import DEFAULT_BPM, MoraNote, build_project, write_srt
from .kana import split_fine_moras, split_moras, vowel_of
from .project import Project
from .ruby import strip_ruby
from .semantic_lyrics import (
    RecognitionBoundaryMerge,
    SemanticLyricDecision,
    is_pathological_repeated_vocalization,
    normalize_repeated_vocalization,
    unowned_note_recovery_windows,
)
from .transcribe import DEFAULT_WHISPER_MODEL, TranscribedLine

if TYPE_CHECKING:
    from .audio_melody import MelodyNote
    from .mora_align import AlignedMora

logger = logging.getLogger(__name__)

ANALYZE_DIR = "analyze_audio"
SEPARATION_DIR = "separation"
# VOICEVOX needs roughly this much time for an independent mora to remain
# intelligible.  Keep this in sync with voicevox.MIN_ARTICULATION_MORA_SEC without
# importing the synthesis backend into the analysis stage.
SPOKEN_SLOT_MIN_SEC = 0.12
# Do not carry a possibly distant or extreme melody pitch into leading/trailing
# speech. C4 is inside the stable singing range and keeps the fallback neutral.
EDGE_SPEECH_MIDI_PITCH = 60


def _torch_device(device: str | None) -> str:
    """Resolve local torch inference placement without loading an audio model."""
    if device is not None:
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _require_audio_pipeline() -> None:
    try:
        from wav_to_xf.pipeline import run_stage3_document  # noqa: F401
        from wav_to_xf.realization import compile_realization  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "音源解析にはwav-to-xfパッケージが必要です。"
            "利用可能なローカルチェックアウトを uv pip install <checkout> で追加してください。"
        ) from exc


def _record_stage3_failure(project_dir: Path, detail: str) -> None:
    """Persist an unresolved Stage 3 decision before failing clearly."""
    analysis_path = project_dir / ANALYZE_DIR / "analysis.json"
    analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis_data["stage3_correspondence"] = False
    analysis_data["limitations"].append(detail)
    analysis_data["diagnostics"].append(
        {"stage": "stage3", "status": "unresolved", "detail": detail}
    )
    analysis_path.write_text(
        json.dumps(analysis_data, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _omit_unresolved_synthesis_units(layers: dict) -> int:
    """Turn pitchless Stage 3 units into explicit, provenance-backed omissions."""
    unresolved = list(dict.fromkeys(layers.get("unresolved_unit_ids", [])))
    if not unresolved:
        return 0
    units = {item["singing_unit_id"] for item in layers.get("performed", [])}
    rendered = {item["singing_unit_id"] for item in layers.get("synthesis_plan", [])}
    existing = {item["singing_unit_id"] for item in layers.get("omissions", [])}
    if any(unit_id not in units or unit_id in rendered for unit_id in unresolved):
        raise ValueError("Stage 3の未解決歌唱単位が合成計画と矛盾しています")

    evidence = list(layers.get("evidence", []))
    layers["evidence"] = evidence
    occupied = {item["id"] for item in evidence}
    omissions = list(layers.get("omissions", []))
    layers["omissions"] = omissions
    for unit_id in unresolved:
        if unit_id in existing:
            continue
        base = f"stage3-synthesis-omission-{unit_id}"
        evidence_id = base
        suffix = 1
        while evidence_id in occupied:
            evidence_id = f"{base}-{suffix}"
            suffix += 1
        occupied.add(evidence_id)
        evidence.append({
            "id": evidence_id,
            "source": "stage3",
            "kind": "synthesis-omission",
            "confidence": 1.0,
            "detail": {"reason": "pitch-unresolved"},
        })
        omissions.append({
            "singing_unit_id": unit_id,
            "reason": "Stage 3で音高を確定できないため合成から省略",
            "evidence_ids": [evidence_id],
        })
    layers["unresolved_unit_ids"] = []
    diagnostics = list(layers.get("diagnostics", []))
    layers["diagnostics"] = diagnostics
    diagnostics.append({
        "stage": "stage3",
        "status": "synthesis-omission",
        "unit_count": len(unresolved),
    })
    return len(unresolved)


def _recover_synthesis_units(
    layers: dict,
    *,
    edge_spoken_utterance_ids: set[str] | None = None,
) -> int:
    """Voice safe pitch gaps without pretending they are measured notes.

    Stage 3 deliberately leaves a unit unresolved when SheetSage has no candidate.
    Short spoken/rap passages can nevertheless have reliable CTC mora intervals
    between two measured melody notes. Keep those intervals and borrow the pitch
    of the nearer bracketing slot solely as a synthesis-compatible ``spoken``
    value.

    Automatic-transcription utterances explicitly supported by vocal-stem activity
    may also be retained at the leading or trailing edge of a song. Those use a
    neutral pitch rather than carrying a potentially distant melody note. Callers
    must opt individual utterances into this edge behavior; known lyrics and audio
    without separated-vocal evidence therefore retain the conservative default.
    """
    edge_spoken_utterance_ids = edge_spoken_utterance_ids or set()
    unresolved = list(dict.fromkeys(layers.get("unresolved_unit_ids", [])))
    if not unresolved:
        return 0

    performed = {
        item["singing_unit_id"]: item for item in layers.get("performed", [])
    }
    plan = list(layers.get("synthesis_plan", []))
    layers["synthesis_plan"] = plan
    anchors = sorted(
        plan, key=lambda item: (item["start_sec"], item["end_sec"], item["id"])
    )
    if not anchors:
        return 0

    mora_details: dict[str, tuple[str, str]] = {}
    for line in layers.get("canonical", []):
        moras = split_fine_moras(line["kana"])
        mora_ids = line["mora_ids"]
        if len(moras) != len(mora_ids):
            raise ValueError("完全歌詞のモーラIDと読みが一致しません")
        for mora_id, kana in zip(mora_ids, moras, strict=True):
            mora_details[mora_id] = (line["utterance_id"], kana)

    omitted = {
        item["singing_unit_id"] for item in layers.get("omissions", [])
    }
    fixed_occupied = [
        (float(item["start_sec"]), float(item["end_sec"])) for item in plan
    ]
    recovered_occupied: list[tuple[float, float, str]] = []
    evidence = list(layers.get("evidence", []))
    layers["evidence"] = evidence
    evidence_ids = {item["id"] for item in evidence}
    recovered: list[str] = []

    for unit_id in unresolved:
        unit = performed.get(unit_id)
        if unit is None or unit_id in omitted:
            continue
        start, end = unit.get("start_sec"), unit.get("end_sec")
        if (not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start + end) or end <= start):
            continue
        if any(left < end and start < right for left, right in fixed_occupied):
            continue
        before = [item for item in anchors if item["end_sec"] <= start]
        after = [item for item in anchors if item["start_sec"] >= end]
        details = [mora_details.get(mora_id) for mora_id in unit["mora_ids"]]
        if not details or any(item is None for item in details):
            continue
        utterances = {item[0] for item in details if item is not None}
        if len(utterances) != 1:
            continue
        utterance_id = utterances.pop()
        kana = "".join(item[1] for item in details if item is not None)

        overlapping_recoveries = {
            owner
            for left, right, owner in recovered_occupied
            if left < end and start < right
        }
        if overlapping_recoveries and overlapping_recoveries != {utterance_id}:
            continue

        if before and after:
            left, right = before[-1], after[0]
            center = (start + end) / 2
            anchor = min(
                (left, right),
                key=lambda item: abs(
                    center
                    - (float(item["start_sec"]) + float(item["end_sec"])) / 2
                ),
            )
            midi_pitch = anchor["midi_pitch"]
            note_candidate_id = anchor.get("note_candidate_id")
            operation = "spoken_pitch_carry"
            recovery_detail = {
                "reason": "bracketed-sheet-sage-gap",
                "pitch_strategy": "nearest-bracketing-slot",
                "anchor_slot_id": anchor["id"],
                "left_slot_id": left["id"],
                "right_slot_id": right["id"],
            }
        elif (before or after) and utterance_id in edge_spoken_utterance_ids:
            edge = "trailing" if before else "leading"
            midi_pitch = EDGE_SPEECH_MIDI_PITCH
            note_candidate_id = None
            operation = "spoken_neutral_pitch"
            recovery_detail = {
                "reason": f"vocal-activity-supported-{edge}-speech",
                "pitch_strategy": "neutral-spoken-midi",
                "midi_pitch": EDGE_SPEECH_MIDI_PITCH,
                "nearest_song_slot_id": (before[-1] if before else after[0])["id"],
            }
        else:
            continue

        evidence_id = f"stage3-spoken-recovery-{unit_id}"
        suffix = 1
        while evidence_id in evidence_ids:
            evidence_id = f"stage3-spoken-recovery-{unit_id}-{suffix}"
            suffix += 1
        evidence_ids.add(evidence_id)
        evidence.append({
            "id": evidence_id,
            "source": "soramimic-video",
            "kind": "spoken-synthesis-fallback",
            "confidence": 1.0,
            "detail": recovery_detail,
        })
        plan.append({
            "id": f"spoken-slot-{unit_id}",
            "utterance_id": utterance_id,
            "singing_unit_id": unit_id,
            "mora_ids": list(unit["mora_ids"]),
            "note_candidate_id": note_candidate_id,
            "link_ids": list(unit.get("link_ids", [])),
            "kana": kana,
            "start_sec": float(start),
            "end_sec": float(end),
            "midi_pitch": midi_pitch,
            "operation": operation,
            "timing_source": "mora_ctc_interval",
            "confidence": 0.0,
            "evidence_ids": [evidence_id],
            "pitch_sources": ["spoken"],
            "pitch_confidence": None,
            "continuation": False,
        })
        recovered_occupied.append((float(start), float(end), utterance_id))
        recovered.append(unit_id)

    if not recovered:
        return 0
    plan.sort(key=lambda item: (item["start_sec"], item["end_sec"], item["id"]))
    recovered_set = set(recovered)
    layers["unresolved_unit_ids"] = [
        unit_id for unit_id in unresolved if unit_id not in recovered_set
    ]
    diagnostics = list(layers.get("diagnostics", []))
    layers["diagnostics"] = diagnostics
    diagnostics.append({
        "stage": "stage3",
        "status": "spoken-synthesis-recovery",
        "unit_count": len(recovered),
    })
    return len(recovered)


def _continuize_spoken_synthesis_lines(layers: dict) -> tuple[int, int]:
    """Give spoken-fallback lines continuous, articulation-safe timing.

    A Stage 3 note candidate can land well after its performed lyric unit while
    the intervening spoken units retain only very short CTC peaks.  Such a score
    technically contains the lyrics but does not articulate them.  Retime every
    rendered slot in an affected line across the complete observed line span,
    absorbing omitted units into the neighbouring rendered lyrics.  Preserve the
    CTC rhythm where possible, but redistribute time so each rendered slot gets an
    audible minimum duration.  Trim simple overlaps at line boundaries without
    crossing another rendered line; ambiguous interior overlaps keep their
    original timing.
    """
    plan = list(layers.get("synthesis_plan", []))
    layers["synthesis_plan"] = plan
    spoken_utterances = {
        item["utterance_id"]
        for item in plan
        if "spoken" in item.get("pitch_sources", [])
    }
    if not spoken_utterances:
        return 0, 0

    canonical = {item["utterance_id"]: item for item in layers.get("canonical", [])}
    performed = list(layers.get("performed", []))
    slots_by_utterance: dict[str, list[dict]] = {}
    for slot in plan:
        slots_by_utterance.setdefault(slot["utterance_id"], []).append(slot)

    evidence = list(layers.get("evidence", []))
    layers["evidence"] = evidence
    evidence_ids = {item["id"] for item in evidence}
    adjusted_lines = 0
    adjusted_slots = 0

    for utterance_id in sorted(spoken_utterances):
        line = canonical.get(utterance_id)
        slots = slots_by_utterance.get(utterance_id, [])
        if line is None or not slots:
            continue
        mora_order = {mora_id: index for index, mora_id in enumerate(line["mora_ids"])}
        units = [
            unit
            for unit in performed
            if unit.get("mora_ids")
            and all(mora_id in mora_order for mora_id in unit["mora_ids"])
        ]
        units_by_id = {unit["singing_unit_id"]: unit for unit in units}
        rendered_ids = [slot["singing_unit_id"] for slot in slots]
        if (
            len(rendered_ids) != len(set(rendered_ids))
            or any(unit_id not in units_by_id for unit_id in rendered_ids)
        ):
            continue
        units.sort(key=lambda unit: min(mora_order[mora_id] for mora_id in unit["mora_ids"]))
        slots.sort(
            key=lambda slot: min(
                mora_order[mora_id] for mora_id in units_by_id[
                    slot["singing_unit_id"]
                ]["mora_ids"]
            )
        )
        observed_bounds = [
            (unit.get("start_sec"), unit.get("end_sec")) for unit in units
        ]
        if any(
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start + end)
            or end <= start
            for start, end in observed_bounds
        ):
            continue
        line_start = float(min(start for start, _end in observed_bounds))
        line_end = float(max(end for _start, end in observed_bounds))
        if line_end <= line_start:
            continue
        blocked = False
        for slot in plan:
            if slot["utterance_id"] == utterance_id:
                continue
            outside_start = float(slot["start_sec"])
            outside_end = float(slot["end_sec"])
            if outside_start >= line_end or outside_end <= line_start:
                continue
            if outside_start <= line_start < outside_end < line_end:
                line_start = outside_end
            elif line_start < outside_start < line_end <= outside_end:
                line_end = outside_start
            else:
                blocked = True
                break
        if blocked or line_end <= line_start:
            continue

        raw_boundaries = [line_start]
        for slot in slots[1:]:
            observed_start = float(
                units_by_id[slot["singing_unit_id"]]["start_sec"]
            )
            raw_boundaries.append(
                max(raw_boundaries[-1], min(line_end, observed_start))
            )
        raw_boundaries.append(line_end)
        raw_durations = [
            max(0.0, right - left)
            for left, right in zip(
                raw_boundaries, raw_boundaries[1:], strict=False
            )
        ]
        total = line_end - line_start
        minimum = min(SPOKEN_SLOT_MIN_SEC, total / len(slots))
        remaining = max(0.0, total - minimum * len(slots))
        flexible = [max(0.0, duration - minimum) for duration in raw_durations]
        flexible_total = sum(flexible)
        if flexible_total > 0.0:
            durations = [
                minimum + remaining * weight / flexible_total
                for weight in flexible
            ]
        else:
            durations = [total / len(slots)] * len(slots)
        candidate: dict[str, tuple[float, float]] = {}
        cursor = line_start
        for index, (slot, duration) in enumerate(
            zip(slots, durations, strict=True)
        ):
            end = line_end if index + 1 == len(slots) else cursor + duration
            candidate[slot["singing_unit_id"]] = (cursor, end)
            cursor = end

        evidence_id = f"stage3-spoken-continuous-{utterance_id}"
        suffix = 1
        while evidence_id in evidence_ids:
            evidence_id = f"stage3-spoken-continuous-{utterance_id}-{suffix}"
            suffix += 1
        evidence_ids.add(evidence_id)
        evidence.append({
            "id": evidence_id,
            "source": "soramimic-video",
            "kind": "spoken-continuous-timing",
            "confidence": 1.0,
            "detail": {
                "reason": "audible-spoken-fallback-line",
                "utterance_id": utterance_id,
                "performed_unit_count": len(units),
                "rendered_unit_count": len(slots),
                "minimum_slot_sec": minimum,
            },
        })
        for slot in slots:
            start, end = candidate[slot["singing_unit_id"]]
            slot["timing_original_start_sec"] = float(slot["start_sec"])
            slot["timing_original_end_sec"] = float(slot["end_sec"])
            slot["start_sec"] = start
            slot["end_sec"] = end
            slot["timing_source"] = "mora_ctc_continuous"
            slot["timing_adjustment"] = "spoken_line_continuous"
            slot["evidence_ids"] = list(slot.get("evidence_ids", [])) + [evidence_id]
        adjusted_lines += 1
        adjusted_slots += len(slots)

    if adjusted_lines:
        plan.sort(key=lambda item: (item["start_sec"], item["end_sec"], item["id"]))
        diagnostics = list(layers.get("diagnostics", []))
        layers["diagnostics"] = diagnostics
        diagnostics.append({
            "stage": "stage3",
            "status": "spoken-continuous-timing",
            "line_count": adjusted_lines,
            "unit_count": adjusted_slots,
        })
    return adjusted_lines, adjusted_slots


def _generation_quality_assessment(layers: dict) -> dict[str, object]:
    """Summarize whether missing melody evidence affects much of the song."""
    performed = {
        item.get("singing_unit_id")
        for item in layers.get("performed", [])
        if item.get("singing_unit_id")
    }
    unsupported = {
        item.get("singing_unit_id")
        for item in layers.get("synthesis_plan", [])
        if item.get("singing_unit_id")
        and "sheetsage2-vocal" not in item.get("pitch_sources", [])
    }
    unsupported.update(
        item.get("singing_unit_id")
        for item in layers.get("omissions", [])
        if item.get("singing_unit_id")
    )
    affected = len(unsupported & performed)
    total = len(performed)
    affected_ratio = affected / total if total else 0.0
    # One isolated gap should not condemn a whole song. Warn when melody evidence
    # is absent for multiple units and a material share of the performance.
    warning = affected >= 2 and affected_ratio >= 0.2
    return {
        "status": "warning" if warning else "ok",
        "reason": "insufficient-melody-coverage" if warning else None,
        "affected_singing_units": affected,
        "total_singing_units": total,
        "affected_ratio": round(affected_ratio, 4),
    }


def _run_sheetsage(
    audio_path: Path,
    project_dir: Path,
    device: str,
    on_progress: Callable[[float], None],
) -> list[MelodyNote] | None:
    from .audio_melody import transcribe_sheetsage

    raw_dir = project_dir / ANALYZE_DIR / "sheetsage-work"
    try:
        return transcribe_sheetsage(
            audio_path, raw_dir, device=device, on_progress=on_progress,
        )
    finally:
        # Never retain detailed model output after success, failure, or cancel.
        shutil.rmtree(raw_dir, ignore_errors=True)


def _run_audio_models(
    audio_path: Path,
    project_dir: Path,
    whisper_model: str,
    whisper_device: str,
    sheetsage_device: str,
    on_sheetsage_progress: Callable[[float], None],
    *,
    run_separation: bool,
    run_whisper: bool,
    shared_inference: bool,
) -> tuple[
    Path,
    Path | None,
    list[TranscribedLine] | None,
    list[MelodyNote] | None,
]:
    """Submit independent audio-analysis jobs before waiting on dependencies.

    The shared loopback service performs its own priority/capacity admission. Local
    fallback uses one worker so this dependency-level concurrency cannot overlap CUDA
    model execution and recreate the historical OOM failure.
    """
    from .separation import separate
    from .transcribe import transcribe_lines

    with ThreadPoolExecutor(
        max_workers=3 if shared_inference else 1,
        thread_name_prefix="audio-analysis",
    ) as executor:
        separation_future = (
            executor.submit(separate, audio_path, project_dir / SEPARATION_DIR)
            if run_separation
            else None
        )
        whisper_future = (
            executor.submit(
                transcribe_lines,
                audio_path,
                whisper_model,
                whisper_device,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            if run_whisper
            else None
        )
        sheetsage_future = executor.submit(
            _run_sheetsage,
            audio_path,
            project_dir,
            sheetsage_device,
            on_sheetsage_progress,
        )
        vocals, accompaniment = (
            separation_future.result()
            if separation_future is not None
            else (audio_path, None)
        )
        lines = whisper_future.result() if whisper_future is not None else None
        notes = sheetsage_future.result()
    return vocals, accompaniment, lines, notes


def _alignment_line_windows(aligned, line_count: int) -> list[tuple[float, float]]:
    bounds: list[tuple[float, float] | None] = [None] * line_count
    for mora in aligned:
        current = bounds[mora.line]
        if current is None:
            bounds[mora.line] = (mora.start_sec, mora.end_sec)
        else:
            bounds[mora.line] = (
                min(current[0], mora.start_sec),
                max(current[1], mora.end_sec),
            )
    if any(bound is None for bound in bounds):
        raise RuntimeError("KanaWhisper用の歌詞行区間を取得できませんでした")
    return [bound for bound in bounds if bound is not None]


def _recognized_line_windows(
    lines: list[TranscribedLine],
) -> list[tuple[float, float]]:
    """Remove only floating-point dust from otherwise touching Whisper lines."""
    windows = [(line.start_sec, line.end_sec) for line in lines]
    for index in range(len(windows) - 1):
        start, end = windows[index]
        next_start, _next_end = windows[index + 1]
        if next_start < end and end - next_start <= 1e-6:
            windows[index] = (start, next_start)
    return windows


def _has_kana_choice(variants: list[list[list[str]]]) -> bool:
    """Whether KanaWhisper has more than one closed reading to compare.

    Connected speech, weak forms, and other real pronunciation variants may change
    the mora count.  Candidate length is therefore evidence to compare, not a gate
    on whether comparison is allowed.
    """
    return any(len(options) > 1 for options in variants)


def _choose_readings_with_kana(
    audio_path: Path,
    vocals_path: Path,
    line_texts: list[str],
    line_variants: list[list[list[str]]],
    line_windows: list[tuple[float, float]],
    *,
    device: str,
    shared_inference: bool,
) -> tuple[list[int], dict[str, object]]:
    import soundfile as sf

    from .kana_whisper import (
        KANA_WHISPER_MODEL,
        KANA_WHISPER_REVISION,
        build_kana_contexts,
        choose_reading,
        transcribe_kana_windows,
    )

    duration = float(sf.info(str(audio_path)).duration)
    contexts, assignments = build_kana_contexts(
        line_windows,
        audio_duration=duration,
    )
    windows = [(context.start_sec, context.end_sec) for context in contexts]
    sources = [("original-mix", audio_path)]
    if vocals_path != audio_path:
        sources.append(("separated-vocals", vocals_path))
    outputs: dict[str, list[str]] = {}
    if windows:
        with ThreadPoolExecutor(
            max_workers=len(sources) if shared_inference else 1,
            thread_name_prefix="kana-whisper",
        ) as executor:
            pending = {
                name: executor.submit(transcribe_kana_windows, path, windows, device)
                for name, path in sources
            }
            outputs = {name: future.result() for name, future in pending.items()}

    selected: list[int] = []
    lines = []
    for index, (text, variants, context_index) in enumerate(
        zip(line_texts, line_variants, assignments, strict=True)
    ):
        candidate_texts = ["".join(candidate) for candidate in variants]
        evidence = (
            [outputs[name][context_index] for name, _path in sources]
            if context_index is not None
            else []
        )
        decision = choose_reading(candidate_texts, evidence)
        selected.append(decision.selected_index)
        lines.append(
            {
                "line": index,
                "surface": strip_ruby(text),
                "candidates": candidate_texts,
                "selected_index": decision.selected_index,
                "reason": decision.reason,
                "context_index": context_index,
                "normalized_evidence": list(decision.normalized_evidence),
                "distances": [list(row) for row in decision.distances],
                "normalized_distances": [
                    list(row) for row in decision.normalized_distances
                ],
            }
        )
    return selected, {
        "schema_version": 3,
        "mode": "closed-reading-candidate-rerank",
        "distance_metric": "kanasim-weighted-substring-0.0.11",
        "model": {
            "id": KANA_WHISPER_MODEL,
            "revision": KANA_WHISPER_REVISION,
        },
        "sources": [name for name, _path in sources],
        "contexts": [
            {
                "start_sec": context.start_sec,
                "end_sec": context.end_sec,
                "line_indices": list(context.line_indices),
                "transcripts": {name: outputs[name][context_index] for name, _path in sources},
            }
            for context_index, context in enumerate(contexts)
        ],
        "lines": lines,
    }


def analyze_audio(
    audio_path: Path,
    project_dir: Path,
    lyrics_path: Path | None = None,
    bpm: float = DEFAULT_BPM,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    skip_separation: bool = False,
    device: str | None = None,
    progress: Callable[[float], None] | None = None,
) -> Project:
    from .mora_align import (
        CTCWindowCapacityError,
        align_moras_with_variants,
        retry_pathological_line_alignments,
    )
    from .reading import automatic_reading_candidates, reading_candidates

    _require_audio_pipeline()
    emissions = None
    recognized_windows = None
    recognition_mode = None
    recognition_windows_fallback = False
    decisions: list[SemanticLyricDecision] = []
    recognition_lines: list[TranscribedLine] = []
    retained_indices: list[int] = []
    retained_lines: list[TranscribedLine] = []
    boundary_merges: list[RecognitionBoundaryMerge] = []
    localized_recoveries: list[dict[str, object]] = []
    localized_deficit_recoveries: list[dict[str, object]] = []
    unowned_note_recoveries: list[dict[str, object]] = []
    vocalization_normalizations: list[dict[str, object]] = []
    localized_alignment_retries: list[dict[str, object]] = []
    ctc_capacity_rejections: list[dict[str, object]] = []
    vocal_activity_profile = None

    last_progress = 0.0

    def report(value: float) -> None:
        nonlocal last_progress
        from . import runproc

        runproc.raise_if_cancelled()
        last_progress = max(last_progress, max(0.0, min(1.0, value)))
        if progress is not None:
            progress(last_progress)

    report(0.01)
    prefetched_lines = None
    sheetsage_notes = None
    sheetsage_was_run = False
    sheetsage_device = _torch_device(device)
    from .audio_inference import configured_url

    shared_inference = configured_url() is not None
    from .audio_melody import configured_capabilities

    capabilities = configured_capabilities()
    # The shared service resolves automatic placement against its own GPU.
    if shared_inference and device is None:
        sheetsage_device = "auto"

    # Audio-analysis jobs depend only on the uploaded mix. Submit them before waiting:
    # Demucs vocals unblock CTC later, while its no_vocals output is retained for mix.
    # Known lyrics deliberately omit Whisper. The loopback service serializes CUDA
    # work according to its existing single-worker priority/capacity policy.
    accompaniment: Path | None = None
    prefetch_audio_models = capabilities["sheetsage2"]
    if prefetch_audio_models:
        logger.info("Demucs/Whisper/SheetSage2の独立ジョブを投入します")
        vocals, accompaniment, prefetched_lines, sheetsage_notes = (
            _run_audio_models(
                audio_path,
                project_dir,
                whisper_model,
                device or "auto",
                sheetsage_device,
                lambda value: report(0.01 + value * 0.47),
                run_separation=not skip_separation,
                run_whisper=lyrics_path is None,
                shared_inference=shared_inference,
            )
        )
        sheetsage_was_run = True
    elif skip_separation:
        vocals = audio_path
        logger.info("音源分離をスキップ(入力をそのままボーカルとして扱います)")
    else:
        from .separation import separate

        vocals, accompaniment = separate(audio_path, project_dir / SEPARATION_DIR)
    report(0.22)

    def normalize_vocalization_line(
        line: TranscribedLine,
        *,
        phase: str,
        source_segment_index: int | None,
    ) -> tuple[TranscribedLine, bool]:
        if sheetsage_notes is None:
            raise RuntimeError("反復発声の正規化にSheetSage2ノートがありません")
        normalization = normalize_repeated_vocalization(line, sheetsage_notes)
        if normalization is None:
            return line, False
        vocalization_normalizations.append({
            "phase": phase,
            "source_segment_index": source_segment_index,
            "start_sec": line.start_sec,
            "end_sec": line.end_sec,
            "original_surface": line.text,
            "surface": normalization.line.text,
            "unit_moras": list(normalization.unit_moras),
            "original_mora_count": normalization.original_mora_count,
            "normalized_mora_count": normalization.normalized_mora_count,
            "note_count": normalization.note_count,
            "capped": (
                normalization.normalized_mora_count
                < normalization.original_mora_count
            ),
            "expanded": (
                normalization.normalized_mora_count
                > normalization.original_mora_count
            ),
            "adjustment": (
                "contracted"
                if normalization.normalized_mora_count
                < normalization.original_mora_count
                else "expanded"
                if normalization.normalized_mora_count
                > normalization.original_mora_count
                else "unchanged"
            ),
        })
        return normalization.line, True

    # 2. 歌詞行の決定
    if lyrics_path is not None:
        line_texts = [
            ln.strip() for ln in lyrics_path.read_text(encoding="utf-8").splitlines()
        ]
        line_texts = [ln for ln in line_texts if ln]
        logger.info("元歌詞: %d行 (%s)", len(line_texts), lyrics_path)
    else:
        from .semantic_lyrics import (
            apply_vocal_activity_support,
            coalesce_repeated_suffix_fragments,
            decide_recognized_line,
        )
        from .transcribe import transcribe_lines
        from .vocal_activity import (
            ACTIVE_FRAME_FLOOR_DBFS,
            FRAME_DURATION_SEC,
            LOUD_FRAME_PERCENTILE,
            MAX_RELATIVE_DROP_DB,
            measure_vocal_activity,
        )

        # Unknown lyrics use one complete, deterministic Whisper transcript.
        # CTC remains downstream for mora timing, not for deciding whether
        # recognized text or its deterministic first reading is accepted.
        lines = prefetched_lines if prefetched_lines is not None else transcribe_lines(
            audio_path, whisper_model, device or "auto", vad_filter=False,
            condition_on_previous_text=False,
        )
        lines, boundary_merges = coalesce_repeated_suffix_fragments(lines)
        if sheetsage_notes is None:
            if not sheetsage_was_run:
                sheetsage_notes = _run_sheetsage(
                    audio_path,
                    project_dir,
                    sheetsage_device,
                    lambda value: report(0.22 + value * 0.26),
                )
                sheetsage_was_run = True
            if sheetsage_notes is None:
                raise RuntimeError(
                    "音源解析にはSheetSage2モデル設定が必要です"
                )
        normalized_lines = []
        for index, line in enumerate(lines):
            normalized, _ = normalize_vocalization_line(
                line,
                phase="initial",
                source_segment_index=index,
            )
            normalized_lines.append(normalized)
        lines = normalized_lines
        recognition_lines = lines
        decisions = [decide_recognized_line(line, sheetsage_notes) for line in lines]
        if not skip_separation:
            vocal_activity_profile = measure_vocal_activity(
                vocals,
                [(line.start_sec, line.end_sec) for line in lines],
            )
            decisions = [
                apply_vocal_activity_support(
                    decision,
                    supported=evidence.supported,
                    percentile_dbfs=evidence.percentile_dbfs,
                    relative_db=evidence.relative_db,
                    active_frame_ratio=evidence.active_frame_ratio,
                )
                for decision, evidence in zip(
                    decisions, vocal_activity_profile.lines, strict=True
                )
            ]
        retained_indices = [
            index for index, decision in enumerate(decisions)
            if decision.status != "rejected"
        ]
        retained = [lines[index] for index in retained_indices]
        retained_lines = retained
        line_texts = [line.text for line in retained]
        recognized_windows = _recognized_line_windows(retained)
        recognition_mode = "whisper-mix-semantic-gate"
        if not retained:
            raise RuntimeError("Whisperが採用可能な歌詞を認識できませんでした")
    # 3. カナ化 + forced alignment。正式歌詞がある場合、通常Whisperによる
    # 表層認識を通さない。KanaWhisperは文字列を書き換えず、ルビ・辞書から
    # 得た閉じた発音候補の再順位付けだけに使う。
    # 元歌詞は青空文庫ルビ記法(｜表層《よみ》)で読みを指定できる。カナ化には記法つきの
    # 行を渡し、字幕・表示に使うテキスト(line_texts)は素テキストに直しておく。
    candidate_builder = (
        automatic_reading_candidates if recognition_mode is not None else reading_candidates
    )
    line_variants = [
        [split_moras(kana) for kana in candidate_builder(text)] or [[]]
        for text in line_texts
    ]
    for text, variants in zip(line_texts, line_variants, strict=True):
        if not variants[0]:
            logger.warning("カナ読みが得られない行をスキップ: %r", text)
    line_texts = [strip_ruby(text) for text in line_texts]
    from .mora_align import compute_emissions

    emissions = emissions or compute_emissions(vocals, device)
    def prepare_automatic_alignment(
        current_lines: list[TranscribedLine], phase: str,
    ) -> tuple[
        list[TranscribedLine], list[str], list[tuple[float, float]] | None,
        list[list[list[str]]], list[int], dict[str, object] | None,
        list[list[list[str]]], list[AlignedMora],
    ]:
        """Reject whole infeasible ASR lines and align only the survivors."""
        nonlocal decisions, retained_indices, recognition_windows_fallback
        while current_lines:
            current_texts = [line.text for line in current_lines]
            current_windows = _recognized_line_windows(current_lines)
            current_variants = [
                [split_moras(kana) for kana in candidate_builder(text)] or [[]]
                for text in current_texts
            ]
            for text, variants in zip(
                current_texts, current_variants, strict=True
            ):
                if not variants[0]:
                    logger.warning("カナ読みが得られない行をスキップ: %r", text)
            current_texts = [strip_ruby(text) for text in current_texts]
            current_choices = [0] * len(current_variants)
            current_reading_evidence = None
            if _has_kana_choice(current_variants):
                current_choices, current_reading_evidence = _choose_readings_with_kana(
                    audio_path,
                    vocals,
                    current_texts,
                    current_variants,
                    current_windows,
                    device=device or "auto",
                    shared_inference=shared_inference,
                )
            current_selected = [
                [variants[index]]
                for variants, index in zip(
                    current_variants, current_choices, strict=True
                )
            ]
            try:
                current_aligned, _fixed_choices = align_moras_with_variants(
                    vocals,
                    current_selected,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                    line_windows=current_windows,
                )
            except CTCWindowCapacityError as exc:
                if exc.line is None or not 0 <= exc.line < len(current_lines):
                    raise
                rejected_line = current_lines[exc.line]
                original_index = next(
                    (
                        index for index, original in enumerate(recognition_lines)
                        if original is rejected_line
                    ),
                    None,
                )
                ctc_capacity_rejections.append({
                    "phase": phase,
                    "source_segment_index": original_index,
                    "source_retained_index": exc.line,
                    "start_sec": rejected_line.start_sec,
                    "end_sec": rejected_line.end_sec,
                    "surface": rejected_line.text,
                    "status": "rejected",
                    "reason": "ctc-window-capacity-insufficient",
                    "available_frames": exc.available_frames,
                    "required_frames": exc.required_frames,
                    "target_count": exc.target_count,
                    "adjacent_repeats": exc.adjacent_repeats,
                })
                if original_index is not None:
                    decisions[original_index] = replace(
                        decisions[original_index], status="rejected"
                    )
                    retained_indices = [
                        index for index in retained_indices
                        if index != original_index
                    ]
                logger.warning(
                    "Whisper行%dをCTC容量不足のため不採用にします "
                    "(%d/%dフレーム)",
                    exc.line, exc.available_frames, exc.required_frames,
                )
                current_lines = [
                    line for index, line in enumerate(current_lines)
                    if index != exc.line
                ]
                continue
            except ValueError as exc:
                recognition_windows_fallback = True
                logger.warning(
                    "Whisper行時刻をCTC整列に使えないため全体整列へ切替: %s",
                    exc,
                )
                current_aligned, _fixed_choices = align_moras_with_variants(
                    vocals,
                    current_selected,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                    line_windows=None,
                )
                current_aligned, retries = retry_pathological_line_alignments(
                    vocals,
                    current_selected,
                    current_aligned,
                    current_windows,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                )
                localized_alignment_retries.extend(retries)
                return (
                    current_lines, current_texts, None, current_variants,
                    current_choices, current_reading_evidence, current_selected,
                    current_aligned,
                )
            recognition_windows_fallback = False
            return (
                current_lines, current_texts, current_windows, current_variants,
                current_choices, current_reading_evidence, current_selected,
                current_aligned,
            )
        raise RuntimeError("Whisperが採用可能な歌詞を認識できませんでした")

    if recognition_mode is not None:
        (
            retained_lines, line_texts, recognized_windows, line_variants,
            chosen, reading_evidence, selected_variants, aligned,
        ) = prepare_automatic_alignment(retained_lines, "initial")
    else:
        default_variants = [[variants[0]] for variants in line_variants]
        initial_alignment, _ = align_moras_with_variants(
            vocals,
            default_variants,
            device=device,
            emissions=emissions,
            phonetic_aliases=True,
            line_windows=None,
        )
        evidence_windows = _alignment_line_windows(
            initial_alignment, len(line_variants)
        )
        reading_evidence = None
        chosen = [0] * len(line_variants)
        if _has_kana_choice(line_variants):
            chosen, reading_evidence = _choose_readings_with_kana(
                audio_path,
                vocals,
                line_texts,
                line_variants,
                evidence_windows,
                device=device or "auto",
                shared_inference=shared_inference,
            )
        selected_variants = [
            [variants[index]]
            for variants, index in zip(line_variants, chosen, strict=True)
        ]
        if not any(chosen):
            aligned = initial_alignment
        else:
            aligned, _fixed_choices = align_moras_with_variants(
                vocals,
                selected_variants,
                device=device,
                emissions=emissions,
                phonetic_aliases=True,
                line_windows=None,
            )

    if recognition_mode is not None:
        from .semantic_lyrics import MIN_CTC_MEDIAN_SCORE, apply_ctc_support

        if sheetsage_notes is None:
            raise RuntimeError("自動歌詞認識にSheetSage2ノートがありません")
        updated = list(decisions)
        for local_index, original_index in enumerate(retained_indices):
            updated[original_index] = apply_ctc_support(
                updated[original_index], local_index, aligned
            )
        decisions = updated
        kept_local_indices = [
            local_index
            for local_index, original_index in enumerate(retained_indices)
            if decisions[original_index].status != "rejected"
        ]
        if len(kept_local_indices) != len(retained_indices):
            from .semantic_lyrics import (
                credit_recovery_windows,
                decide_recognized_line,
            )
            from .transcribe import transcribe_window

            recovered_lines: list[TranscribedLine] = []
            for original_index in retained_indices:
                decision = decisions[original_index]
                if decision.status != "rejected" or decision.ctc_support is not False:
                    continue
                original_line = recognition_lines[original_index]
                for start_sec, end_sec in credit_recovery_windows(
                    original_line, sheetsage_notes
                ):
                    candidates = transcribe_window(
                        audio_path,
                        start_sec,
                        end_sec,
                        whisper_model,
                        device or "auto",
                    )
                    accepted = []
                    for candidate in candidates:
                        candidate, _ = normalize_vocalization_line(
                            candidate,
                            phase="semantic-recovery",
                            source_segment_index=original_index,
                        )
                        candidate_decision = decide_recognized_line(
                            candidate, sheetsage_notes
                        )
                        if (
                            candidate_decision.template_family is None
                            and candidate_decision.melodic_support
                        ):
                            accepted.append(candidate)
                    recovered_lines.extend(accepted)
                    localized_recoveries.append({
                        "source_segment_index": original_index,
                        "start_sec": start_sec,
                        "end_sec": end_sec,
                        "status": "accepted" if accepted else "unresolved",
                        "segments": [
                            {
                                "start_sec": item.start_sec,
                                "end_sec": item.end_sec,
                                "surface": item.text,
                            }
                            for item in accepted
                        ],
                    })

            retained_indices = [
                index for index in retained_indices
                if decisions[index].status != "rejected"
            ]
            retained_lines = sorted(
                [recognition_lines[index] for index in retained_indices]
                + recovered_lines,
                key=lambda line: (line.start_sec, line.end_sec),
            )
            if not retained_lines:
                raise RuntimeError("Whisperが採用可能な歌詞を認識できませんでした")
            # The final retained-line alignment replaces the screening pass and
            # therefore owns the retry provenance recorded below.
            localized_alignment_retries = []
            (
                retained_lines, line_texts, recognized_windows, line_variants,
                chosen, reading_evidence, selected_variants, aligned,
            ) = prepare_automatic_alignment(retained_lines, "semantic-recovery")

        from .semantic_lyrics import lyric_deficit_recoveries
        from .transcribe import transcribe_window

        deficit_candidates = lyric_deficit_recoveries(
            retained_lines,
            [
                len(variants[choice])
                for variants, choice in zip(line_variants, chosen, strict=True)
            ],
            sheetsage_notes,
        )
        replacements: dict[int, list[TranscribedLine]] = {}
        for recovery in deficit_candidates:
            source_line = retained_lines[recovery.line]
            source_segment_index = next(
                (
                    index
                    for index, original in enumerate(recognition_lines)
                    if original is source_line
                ),
                None,
            )
            recovered_lines = []
            recovered_repetitions = []
            recovered_pathological_repetition = False
            for start_sec, end_sec in recovery.windows:
                candidates = transcribe_window(
                    audio_path,
                    start_sec,
                    end_sec,
                    whisper_model,
                    device or "auto",
                )
                for candidate in candidates:
                    if is_pathological_repeated_vocalization(
                        candidate, recovery.note_count
                    ):
                        recovered_pathological_repetition = True
                    candidate, is_repetition = normalize_vocalization_line(
                        candidate,
                        phase="deficit-recovery",
                        source_segment_index=source_segment_index,
                    )
                    recovered_repetitions.append(is_repetition)
                    recovered_lines.append(candidate)
            recovered_lines.sort(key=lambda line: (line.start_sec, line.end_sec))
            rejection_reasons = []
            recovered_decisions = [
                decide_recognized_line(line, sheetsage_notes)
                for line in recovered_lines
            ]
            if not recovered_lines:
                rejection_reasons.append("empty-transcript")
            if recovered_pathological_repetition:
                # A retry is allowed to preserve the attacks Whisper actually
                # heard, but it must never replace a usable source line with a
                # decoder runaway containing hundreds of periodic syllables.
                rejection_reasons.append("pathological-repetition")
            if any(
                decision.template_family is not None
                for decision in recovered_decisions
            ):
                rejection_reasons.append("non-lyric-template")
            if any(
                not decision.melodic_support
                for decision in recovered_decisions
            ):
                rejection_reasons.append("insufficient-melodic-support")
            pure_vocalization = bool(recovered_lines) and all(
                recovered_repetitions
            )
            recovered_variants = [
                [split_moras(kana) for kana in candidate_builder(line.text)] or [[]]
                for line in recovered_lines
            ]
            if any(not variants[0] for variants in recovered_variants):
                rejection_reasons.append("missing-reading")
            recovered_aligned: list[AlignedMora] = []
            recovered_choices = [0] * len(recovered_variants)
            if not rejection_reasons:
                try:
                    recovered_aligned, recovered_choices = align_moras_with_variants(
                        vocals,
                        recovered_variants,
                        device=device,
                        emissions=emissions,
                        phonetic_aliases=True,
                        line_windows=_recognized_line_windows(recovered_lines),
                    )
                except (RuntimeError, ValueError) as exc:
                    rejection_reasons.append(f"ctc-alignment-failed:{exc}")
            recovered_moras = sum(
                len(variants[choice])
                for variants, choice in zip(
                    recovered_variants, recovered_choices, strict=True
                )
            )
            required_moras = recovery.effective_mora_count + max(
                2, math.ceil(recovery.effective_mora_count * 0.25)
            )
            if not pure_vocalization:
                if recovered_moras < required_moras:
                    rejection_reasons.append("insufficient-detail-gain")
                if recovered_moras > recovery.note_count * 3:
                    rejection_reasons.append("pathological-detail-gain")
            recovered_scores = [mora.score for mora in recovered_aligned]
            source_scores = [
                mora.score for mora in aligned if mora.line == recovery.line
            ]
            recovered_ctc_median = (
                statistics.median(recovered_scores) if recovered_scores else 0.0
            )
            source_ctc_median = (
                statistics.median(source_scores) if source_scores else 0.0
            )
            if not pure_vocalization:
                if recovered_ctc_median < MIN_CTC_MEDIAN_SCORE:
                    rejection_reasons.append("insufficient-ctc-support")
                if (
                    source_ctc_median > 0.0
                    and recovered_ctc_median < source_ctc_median * 0.5
                ):
                    rejection_reasons.append("ctc-weaker-than-source")
            candidate_accepted = not rejection_reasons
            if candidate_accepted:
                replacements[recovery.line] = recovered_lines
            localized_deficit_recoveries.append({
                "source_segment_index": source_segment_index,
                "source_retained_index": recovery.line,
                "source_surface": source_line.text,
                "source_mora_count": recovery.mora_count,
                "source_effective_mora_count": recovery.effective_mora_count,
                "raw_note_count": recovery.note_count,
                "median_notes_per_mora": recovery.median_notes_per_mora,
                "residual_notes": recovery.residual_notes,
                "retry_windows": [list(window) for window in recovery.windows],
                "status": "accepted" if candidate_accepted else "rejected",
                "classification": (
                    "repeated-vocalization" if pure_vocalization else "lyrics"
                ),
                "rejection_reasons": rejection_reasons,
                "recovered_mora_count": recovered_moras,
                "source_ctc_median_score": source_ctc_median,
                "recovered_ctc_median_score": recovered_ctc_median,
                "segments": [
                    {
                        "start_sec": line.start_sec,
                        "end_sec": line.end_sec,
                        "surface": line.text,
                    }
                    for line in recovered_lines
                ],
            })

        if replacements:
            retained_lines = [
                replacement
                for index, line in enumerate(retained_lines)
                for replacement in replacements.get(index, [line])
            ]
            retained_lines.sort(key=lambda line: (line.start_sec, line.end_sec))
            localized_alignment_retries = []
            (
                retained_lines, line_texts, recognized_windows, line_variants,
                chosen, reading_evidence, selected_variants, aligned,
            ) = prepare_automatic_alignment(retained_lines, "deficit-recovery")

    if recognition_mode is not None:
        # Stage 3 is the only component which knows whether a SheetSage note is
        # truly unowned after lyric alignment.  Run it once as a read-only probe,
        # retry only long note-only regions, then rebuild the final correspondence
        # below if a retry adds lyrics.
        from .stage3 import build_stage3_layers
        from .transcribe import transcribe_window

        if sheetsage_notes is None:
            raise RuntimeError("未所有ノート回復にSheetSage2ノートがありません")

        provisional_readings = [
            "".join(variants[index])
            for variants, index in zip(line_variants, chosen, strict=True)
        ]
        provisional_windows = (
            _recognized_line_windows(retained_lines)
            if not recognition_windows_fallback
            else None
        )
        provisional_document, _provisional_layers = build_stage3_layers(
            line_texts,
            provisional_readings,
            aligned,
            sheetsage_notes,
            whisper_line_windows=provisional_windows,
            enable_repeated_vocalization=provisional_windows is not None,
            ctc_emissions=emissions,
        )
        recovery_windows = unowned_note_recovery_windows(
            json.loads(provisional_document.to_json()), retained_lines
        )
        activity_by_window = (
            measure_vocal_activity(
                vocals,
                [(window.start_sec, window.end_sec) for window in recovery_windows],
            ).lines
            if recovery_windows and not skip_separation
            else ()
        )
        recovered_unowned_lines: list[TranscribedLine] = []
        for window_index, window in enumerate(recovery_windows):
            evidence = (
                activity_by_window[window_index]
                if window_index < len(activity_by_window)
                else None
            )
            record: dict[str, object] = {
                "start_sec": window.start_sec,
                "end_sec": window.end_sec,
                "note_count": window.note_count,
                "seed_note_count": window.seed_note_count,
                "note_ids": list(window.note_ids),
                "temperature": 0.0,
                "vocal_activity": (
                    {
                        "supported": evidence.supported,
                        "percentile_dbfs": evidence.percentile_dbfs,
                        "relative_db": evidence.relative_db,
                        "active_frame_ratio": evidence.active_frame_ratio,
                    }
                    if evidence is not None
                    else None
                ),
            }
            if evidence is None or not evidence.supported:
                record.update({
                    "status": "rejected",
                    "rejection_reasons": [
                        "separated-vocal-activity-unavailable"
                        if evidence is None
                        else "insufficient-vocal-activity"
                    ],
                    "attempts": [],
                })
                unowned_note_recoveries.append(record)
                continue

            attempts: list[dict[str, object]] = []
            accepted_attempts: list[
                tuple[float, bool, int, str, list[TranscribedLine]]
            ] = []
            fallback_observations: list[
                tuple[str, list[TranscribedLine], list[str], list[str]]
            ] = []
            for source, retry_start, retry_end in (
                ("exact", window.start_sec, window.end_sec),
                ("padded", max(0.0, window.start_sec - 0.5), window.end_sec + 0.5),
            ):
                raw_candidates = transcribe_window(
                    audio_path,
                    retry_start,
                    retry_end,
                    whisper_model,
                    device or "auto",
                    temperature=0.0,
                )
                candidates = [
                    TranscribedLine(
                        max(window.start_sec, candidate.start_sec),
                        min(window.end_sec, candidate.end_sec),
                        candidate.text,
                    )
                    for candidate in raw_candidates
                    if min(window.end_sec, candidate.end_sec)
                    > max(window.start_sec, candidate.start_sec)
                ]
                normalized_candidates = []
                rejection_reasons = []
                for candidate in candidates:
                    if is_pathological_repeated_vocalization(
                        candidate, window.note_count
                    ):
                        rejection_reasons.append("pathological-repetition")
                        continue
                    candidate, _is_repetition = normalize_vocalization_line(
                        candidate,
                        phase="unowned-note-recovery",
                        source_segment_index=None,
                    )
                    decision = decide_recognized_line(candidate, sheetsage_notes)
                    if decision.template_family is not None:
                        rejection_reasons.append("non-lyric-template")
                    elif not decision.melodic_support:
                        rejection_reasons.append("insufficient-melodic-support")
                    else:
                        normalized_candidates.append(candidate)
                attempt_variants = [
                    [split_moras(kana) for kana in candidate_builder(line.text)] or [[]]
                    for line in normalized_candidates
                ]
                if not normalized_candidates:
                    rejection_reasons.append("empty-transcript")
                if any(not item[0] for item in attempt_variants):
                    rejection_reasons.append("missing-reading")
                aligned_attempt: list[AlignedMora] = []
                if not rejection_reasons:
                    try:
                        aligned_attempt, _fixed_choices = align_moras_with_variants(
                            vocals,
                            [[item[0]] for item in attempt_variants],
                            device=device,
                            emissions=emissions,
                            phonetic_aliases=True,
                            line_windows=_recognized_line_windows(
                                normalized_candidates
                            ),
                        )
                    except (RuntimeError, ValueError) as exc:
                        rejection_reasons.append(f"ctc-alignment-failed:{exc}")
                mora_count = sum(len(item[0]) for item in attempt_variants)
                minimum_moras = max(4, math.ceil(window.note_count * 0.25))
                if mora_count < minimum_moras:
                    rejection_reasons.append("insufficient-detail")
                if mora_count > window.note_count * 2:
                    rejection_reasons.append("excessive-detail")
                median_score = (
                    statistics.median(item.score for item in aligned_attempt)
                    if aligned_attempt
                    else 0.0
                )
                if median_score < MIN_CTC_MEDIAN_SCORE:
                    rejection_reasons.append("insufficient-ctc-support")
                rejection_reasons = list(dict.fromkeys(rejection_reasons))
                attempt_accepted = not rejection_reasons
                fallback_observations.append((
                    source,
                    normalized_candidates,
                    [mora for item in attempt_variants for mora in item[0]],
                    rejection_reasons,
                ))
                attempts.append({
                    "source": source,
                    "start_sec": retry_start,
                    "end_sec": retry_end,
                    "status": "accepted" if attempt_accepted else "rejected",
                    "rejection_reasons": rejection_reasons,
                    "mora_count": mora_count,
                    "ctc_median_score": median_score,
                    "segments": [
                        {
                            "start_sec": line.start_sec,
                            "end_sec": line.end_sec,
                            "surface": line.text,
                        }
                        for line in normalized_candidates
                    ],
                })
                if attempt_accepted:
                    accepted_attempts.append((
                        median_score,
                        source == "exact",
                        -abs(mora_count - window.note_count),
                        source,
                        normalized_candidates,
                    ))

            # If both bounded retries agree on timing and vowels but their
            # consonants cannot clear the CTC gate, retain only the shared vowel
            # continuation.  This runs after Whisper/readings and before the
            # recovered line is admitted to the final alignment.
            if not accepted_attempts and len(fallback_observations) == 2:
                exact, padded = fallback_observations
                exact_vowels = [vowel_of(mora) for mora in exact[2]]
                padded_vowels = [vowel_of(mora) for mora in padded[2]]
                exact_bounds = (
                    (exact[1][0].start_sec, exact[1][-1].end_sec)
                    if exact[1]
                    else None
                )
                padded_bounds = (
                    (padded[1][0].start_sec, padded[1][-1].end_sec)
                    if padded[1]
                    else None
                )
                consonant_only_failure = all(
                    reasons == ["insufficient-ctc-support"]
                    for _source, _lines, _moras, reasons in fallback_observations
                )
                timing_agrees = (
                    exact_bounds is not None
                    and padded_bounds is not None
                    and abs(exact_bounds[0] - padded_bounds[0]) <= 0.6
                    and abs(exact_bounds[1] - padded_bounds[1]) <= 0.6
                )
                if (
                    consonant_only_failure
                    and timing_agrees
                    and exact_vowels == padded_vowels
                    and len(exact_vowels) >= max(
                        4, math.ceil(window.note_count * 0.25)
                    )
                    and all(vowel is not None for vowel in exact_vowels)
                ):
                    vowel_line = TranscribedLine(
                        window.start_sec,
                        window.end_sec,
                        "".join(vowel for vowel in exact_vowels if vowel is not None),
                    )
                    try:
                        vowel_aligned, _fixed_choices = align_moras_with_variants(
                            vocals,
                            [[[vowel for vowel in exact_vowels if vowel is not None]]],
                            device=device,
                            emissions=emissions,
                            phonetic_aliases=True,
                            line_windows=[(window.start_sec, window.end_sec)],
                        )
                    except (RuntimeError, ValueError):
                        vowel_aligned = []
                    vowel_score = (
                        statistics.median(item.score for item in vowel_aligned)
                        if vowel_aligned
                        else 0.0
                    )
                    vowel_accepted = vowel_score >= MIN_CTC_MEDIAN_SCORE
                    attempts.append({
                        "source": "vowel-continuation",
                        "start_sec": window.start_sec,
                        "end_sec": window.end_sec,
                        "status": "accepted" if vowel_accepted else "rejected",
                        "rejection_reasons": (
                            [] if vowel_accepted else ["insufficient-ctc-support"]
                        ),
                        "mora_count": len(exact_vowels),
                        "ctc_median_score": vowel_score,
                        "segments": [{
                            "start_sec": vowel_line.start_sec,
                            "end_sec": vowel_line.end_sec,
                            "surface": vowel_line.text,
                        }],
                    })
                    if vowel_accepted:
                        accepted_attempts.append((
                            vowel_score,
                            False,
                            -abs(len(exact_vowels) - window.note_count),
                            "vowel-continuation",
                            [vowel_line],
                        ))

            if accepted_attempts:
                selected = max(accepted_attempts, key=lambda item: item[:3])
                recovered_unowned_lines.extend(selected[4])
                record.update({
                    "status": "accepted",
                    "selected_source": selected[3],
                    "rejection_reasons": [],
                    "attempts": attempts,
                })
            else:
                record.update({
                    "status": "rejected",
                    "rejection_reasons": ["no-acoustically-supported-retry"],
                    "attempts": attempts,
                })
            unowned_note_recoveries.append(record)

        if recovered_unowned_lines:
            retained_lines = sorted(
                retained_lines + recovered_unowned_lines,
                key=lambda line: (line.start_sec, line.end_sec),
            )
            localized_alignment_retries = []
            (
                retained_lines, line_texts, recognized_windows, line_variants,
                chosen, reading_evidence, selected_variants, aligned,
            ) = prepare_automatic_alignment(retained_lines, "unowned-note-recovery")

    expected_moras = sum(
        len(line[choice])
        for line, choice in zip(line_variants, chosen, strict=True)
    )
    if len(aligned) != expected_moras:
        raise RuntimeError("正式歌詞のモーラをすべてアライメントできませんでした")
    raw_alignment = [replace(mora) for mora in aligned]
    if recognition_mode is not None:
        out = project_dir / ANALYZE_DIR
        out.mkdir(parents=True, exist_ok=True)
        retained = retained_lines
        (out / "recognition.json").write_text(
            json.dumps(
                {
                    "schema_version": 6,
                    "mode": recognition_mode,
                    "model": whisper_model,
                    "transcription_options": {
                        "vad_filter": False,
                        "condition_on_previous_text": False,
                    },
                    "semantic_gate": {
                        "melody_source": "sheetsage2-original-mix",
                        "ctc_source": "reazon-kana-ctc-separated-vocals"
                        if not skip_separation
                        else "reazon-kana-ctc-input-audio",
                        "rule": (
                            "always-recover-exact-credit-patterns; require-melody-"
                            "and-ctc-median-support-for-other-non-lyric-patterns; "
                            "reject-ordinary-nonmelodic-lines-without-relative-"
                            "vocal-stem-activity"
                        ),
                        "ctc_median_threshold": MIN_CTC_MEDIAN_SCORE,
                        "vocal_activity": (
                            {
                                "applied": True,
                                "source": "demucs-separated-vocals",
                                "frame_duration_sec": FRAME_DURATION_SEC,
                                "line_percentile": LOUD_FRAME_PERCENTILE,
                                "active_frame_floor_dbfs": ACTIVE_FRAME_FLOOR_DBFS,
                                "max_relative_drop_db": MAX_RELATIVE_DROP_DB,
                                "reference_dbfs": vocal_activity_profile.reference_dbfs,
                            }
                            if vocal_activity_profile is not None
                            else {
                                "applied": False,
                                "reason": "separation-skipped",
                            }
                        ),
                        "boundary_merges": [
                            {
                                "left_index": item.left_index,
                                "right_index": item.right_index,
                                "left_surface": item.left_surface,
                                "right_surface": item.right_surface,
                                "merged_surface": item.merged_surface,
                            }
                            for item in boundary_merges
                        ],
                        "decisions": [
                            {
                                "start_sec": line.start_sec,
                                "end_sec": line.end_sec,
                                "surface": line.text,
                                "normalized_surface": decision.normalized_text,
                                "status": decision.status,
                                "template_family": decision.template_family,
                                "melodic_support": decision.melodic_support,
                                "ctc_support": decision.ctc_support,
                                "ctc_median_score": decision.ctc_median_score,
                                "vocal_activity_support": (
                                    decision.vocal_activity_support
                                ),
                                "vocal_activity_percentile_dbfs": (
                                    decision.vocal_activity_percentile_dbfs
                                ),
                                "vocal_activity_relative_db": (
                                    decision.vocal_activity_relative_db
                                ),
                                "vocal_active_frame_ratio": (
                                    decision.vocal_active_frame_ratio
                                ),
                            }
                            for line, decision in zip(
                                recognition_lines, decisions, strict=True
                            )
                        ],
                        "localized_recoveries": localized_recoveries,
                        "localized_deficit_recoveries": localized_deficit_recoveries,
                        "unowned_note_recoveries": unowned_note_recoveries,
                        "vocalization_normalizations": vocalization_normalizations,
                        "localized_alignment_retries": localized_alignment_retries,
                        "ctc_capacity_rejections": ctc_capacity_rejections,
                    },
                    "segments": [
                        {
                            "start_sec": line.start_sec,
                            "end_sec": line.end_sec,
                            "surface": line.text,
                        }
                        for line in retained
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        logger.info(
            "Whisper mix/no-VADの%d行を元歌詞として採用 "
            "(意味・音量ゲートで%d行除外、非旋律音声を%d行保持)",
            len(line_texts),
            sum(decision.status == "rejected" for decision in decisions),
            sum(decision.status == "unresolved" for decision in decisions),
        )
    n_ambiguous = sum(1 for v in line_variants if len(v) > 1)
    if n_ambiguous:
        logger.info(
            "読み候補が複数の行: %d行(うち%d行で第2候補以降を採用)",
            n_ambiguous, sum(1 for k in chosen if k != 0),
        )

    report(0.48)

    # Keep the measured CTC interval and delegate pitch entirely to SheetSage/Stage 3.
    report(0.62)

    # 6. モーラ音符列の確定
    if sheetsage_notes is None:
        raise RuntimeError("Stage 3へ渡すSheetSage2ノート候補がありません")
    mora_notes: list[MoraNote] = []
    mode = "sheetsage2_stage3"
    out = project_dir / ANALYZE_DIR
    out.mkdir(parents=True, exist_ok=True)
    if reading_evidence is not None:
        (out / "reading.json").write_text(
            json.dumps(reading_evidence, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    reading_asr_used = bool(reading_evidence is not None and reading_evidence["contexts"])
    recognition_flags = [
        {
            "status": decision.status,
            "normalized_surface": decision.normalized_text,
            "template_family": decision.template_family,
            "melodic_support": decision.melodic_support,
            "ctc_support": decision.ctc_support,
            "ctc_median_score": decision.ctc_median_score,
            "vocal_activity_support": decision.vocal_activity_support,
            "vocal_activity_percentile_dbfs": (
                decision.vocal_activity_percentile_dbfs
            ),
            "vocal_activity_relative_db": decision.vocal_activity_relative_db,
            "vocal_active_frame_ratio": decision.vocal_active_frame_ratio,
        }
        for decision in (decisions if recognition_mode is not None else [])
        if decision.status != "accepted"
    ]
    limitations = []
    if recognition_mode is not None:
        limitations.append(
            "未知歌詞はWhisperによる推定です。recognition.jsonで認識結果を確認できます。"
        )
    if ctc_capacity_rejections:
        limitations.append(
            "Whisper区間のCTCフレームへ収まらない自動認識行を"
            f"{len(ctc_capacity_rejections)}行、不採用にしました。"
        )
    if recognition_windows_fallback:
        repaired_count = sum(
            item.get("status") == "replaced"
            for item in localized_alignment_retries
        )
        detail = (
            f" 病的な{repaired_count}行はWhisper区間内で局所再整列しました。"
            if repaired_count else ""
        )
        limitations.append(
            "Whisperの行時刻をCTC整列に使えなかったため、"
            f"CTC全体整列へ切り替えました。{detail}"
        )
    (out / "analysis.json").write_text(
        json.dumps(
            {
                "schema_version": 5,
                "mode": mode,
                "official_lyrics": lyrics_path is not None,
                "asr_used": lyrics_path is None or reading_asr_used,
                "lyric_asr_used": lyrics_path is None,
                "reading_asr_used": reading_asr_used,
                "audio_pipeline": "stage3",
                "inference_roles": {
                    "lyrics": f"whisper-{whisper_model}-original-mix"
                    if lyrics_path is None
                    else "known-lyrics",
                    "mora_timing": (
                        "reazon-kana-ctc-input-audio"
                        if skip_separation
                        else "reazon-kana-ctc-separated-vocals"
                    ),
                    "reading": (
                        "kana-whisper-closed-candidate-rerank"
                        if reading_asr_used
                        else "yomi-unidic-default-reading"
                    ),
                    "notes": "sheetsage2-original-mix",
                    "separation": "skipped-input-as-vocals"
                    if skip_separation
                    else "demucs",
                },
                "stage3_correspondence": True,
                "recognition_mode": recognition_mode,
                "recognition_flags": recognition_flags,
                "localized_alignment_retries": localized_alignment_retries,
                "ctc_capacity_rejections": ctc_capacity_rejections,
                "mora_count": len(raw_alignment),
                "sources": {},
                "limitations": limitations,
                "diagnostics": [
                    {"stage": "recognition", **flag} for flag in recognition_flags
                ] + [
                    {"stage": "mora-ctc-local-retry", **item}
                    for item in localized_alignment_retries
                ] + [
                    {"stage": "mora-ctc-capacity", **item}
                    for item in ctc_capacity_rejections
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    report(0.97)
    project = build_project(
        audio_path=audio_path,
        vocals_path=None if skip_separation else vocals,
        accompaniment_path=accompaniment,
        line_texts=line_texts,
        mora_notes=mora_notes,
        bpm=bpm,
    )
    from .lyric_layers import apply_lyric_layers

    selected_readings = [
        "".join(variants[index])
        for variants, index in zip(line_variants, chosen, strict=True)
    ]
    from .stage3 import build_stage3_layers

    try:
        whisper_line_windows = (
            _recognized_line_windows(retained_lines)
            if recognition_mode is not None and not recognition_windows_fallback
            else None
        )
        document, layers = build_stage3_layers(
            line_texts, selected_readings, raw_alignment, sheetsage_notes,
            whisper_line_windows=whisper_line_windows,
            enable_repeated_vocalization=whisper_line_windows is not None,
            ctc_emissions=emissions,
        )
    except ValueError as exc:
        detail = (
            "Stage 3の合成計画を確定できませんでした。"
            "歌詞や音高を補わず処理を停止します。"
        )
        _record_stage3_failure(project_dir, detail)
        raise RuntimeError(detail) from exc
    else:
        (out / "correspondence.json").write_text(
            document.to_json(), encoding="utf-8"
        )
    layer_data = layers.to_dict()
    edge_spoken_utterance_ids: set[str] = set()
    if recognition_mode is not None:
        supported_lines = {
            id(line)
            for line, decision in zip(recognition_lines, decisions, strict=True)
            if (
                decision.status == "unresolved"
                and decision.template_family is None
                and decision.vocal_activity_support is True
            )
        }
        edge_spoken_utterance_ids = {
            canonical["utterance_id"]
            for canonical, line in zip(
                layer_data["canonical"], retained_lines, strict=True
            )
            if id(line) in supported_lines
        }
    recovered_units = _recover_synthesis_units(
        layer_data,
        edge_spoken_utterance_ids=edge_spoken_utterance_ids,
    )
    continuous_lines, continuous_units = _continuize_spoken_synthesis_lines(layer_data)
    omitted_units = _omit_unresolved_synthesis_units(layer_data)
    for slot in layer_data["synthesis_plan"]:
        # SheetSage does not expose calibrated pitch confidence. Preserve the
        # candidate source but do not turn its schema-required score into one.
        if "sheetsage2-vocal" in slot.get("pitch_sources", []):
            slot["pitch_confidence"] = None
    apply_lyric_layers(project, layer_data)
    analysis_path = out / "analysis.json"
    analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis_data["sources"] = dict(
        sorted(
            {
                source: sum(note.source == source for note in project.notes)
                for source in {note.source for note in project.notes}
            }.items()
        )
    )
    analysis_data["generation_quality"] = _generation_quality_assessment(layer_data)
    if recovered_units:
        detail = (
            f"安全に補完できる{recovered_units}歌唱単位を、CTC時刻と"
            "spoken合成用音高で保持しました。"
        )
        analysis_data["limitations"].append(detail)
        analysis_data["diagnostics"].append({
            "stage": "stage3",
            "status": "spoken-synthesis-recovery",
            "unit_count": recovered_units,
            "detail": detail,
        })
    if continuous_lines:
        detail = (
            f"spoken補完を含み全歌唱単位が揃う{continuous_lines}行・"
            f"{continuous_units}歌唱単位を、CTC開始位置から次の開始位置まで"
            "連続する合成時刻へ調整しました。"
        )
        analysis_data["limitations"].append(detail)
        analysis_data["diagnostics"].append({
            "stage": "stage3",
            "status": "spoken-continuous-timing",
            "line_count": continuous_lines,
            "unit_count": continuous_units,
            "detail": detail,
        })
    if omitted_units:
        detail = (
            f"Stage 3で音高を確定できなかった{omitted_units}歌唱単位を"
            "推測で補わず、合成から省略しました。"
        )
        analysis_data["limitations"].append(detail)
        analysis_data["diagnostics"].append({
            "stage": "stage3",
            "status": "synthesis-omission",
            "unit_count": omitted_units,
            "detail": detail,
        })
    analysis_path.write_text(
        json.dumps(analysis_data, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    # 目視検証用SRT
    out = project_dir / ANALYZE_DIR
    out.mkdir(parents=True, exist_ok=True)
    write_srt(
        out / "moras.srt",
        [(n.start_sec, n.end_sec, n.kana) for n in project.notes],
    )
    write_srt(
        out / "lines.srt",
        [
            (*project.line_time_range(ln), ln.original_text or ln.xf_kana)
            for ln in project.lines
        ],
    )
    logger.info("検証用SRT: %s", out)
    report(1.0)
    return project
