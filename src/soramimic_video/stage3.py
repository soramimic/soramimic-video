"""Bridge acoustic observations into Soramimic Score correspondence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soramimic_score import IntermediateRepresentation, Realization

    from .audio_melody import MelodyNote
    from .mora_align import AlignedMora, CTCEmissions


_WHISPER_BOUNDARY_COST_PER_SEC2 = 0.1
_WHISPER_REST_MIN_SEC = 0.08
_WHISPER_REST_MAX_SNAP_SEC = 0.75


def _snap_whisper_windows_to_sheetsage_rests(
    windows: Sequence[tuple[float, float]],
    melody_notes: Sequence[MelodyNote],
) -> tuple[tuple[float, float], ...]:
    """Move adjacent Whisper boundaries to nearby SheetSage rest midpoints."""
    starts = [float(start) for start, _end in windows]
    ends = [float(end) for _start, end in windows]
    rests = []
    for left, right in zip(melody_notes, melody_notes[1:], strict=False):
        duration = right.start_sec - left.end_sec
        if duration >= _WHISPER_REST_MIN_SEC:
            midpoint = (left.end_sec + right.start_sec) / 2
            rests.append((midpoint, duration))

    previous_boundary = -math.inf
    for index in range(len(windows) - 1):
        raw_left_end = float(windows[index][1])
        raw_right_start = float(windows[index + 1][0])
        target = (raw_left_end + raw_right_start) / 2
        candidates = [
            (abs(midpoint - target), -duration, midpoint)
            for midpoint, duration in rests
            if abs(midpoint - target) <= _WHISPER_REST_MAX_SNAP_SEC
        ]
        chosen = min(candidates, default=None)
        if chosen is None:
            previous_boundary = max(previous_boundary, ends[index])
            continue
        boundary = chosen[2]
        if (boundary <= starts[index] or boundary >= ends[index + 1]
                or boundary <= previous_boundary):
            previous_boundary = max(previous_boundary, ends[index])
            continue
        ends[index] = boundary
        starts[index + 1] = boundary
        previous_boundary = boundary

    snapped = tuple(zip(starts, ends, strict=True))
    if any(start > end for start, end in snapped):
        raise ValueError("SheetSage休符補正後のWhisper区間が反転しています")
    if any(right[0] < left[1]
           for left, right in zip(snapped, snapped[1:], strict=False)):
        raise ValueError("SheetSage休符補正後のWhisper区間が重複しています")
    return snapped


def build_stage3_layers(
    line_texts: Sequence[str],
    selected_readings: Sequence[str],
    aligned: Sequence[AlignedMora],
    melody_notes: Sequence[MelodyNote],
    *,
    whisper_line_windows: Sequence[tuple[float, float]] | None = None,
    enable_repeated_vocalization: bool = False,
    ctc_emissions: CTCEmissions | None = None,
    fixed_reading_indices: frozenset[int] = frozenset(),
) -> tuple[IntermediateRepresentation, Realization]:
    """Use every SheetSage candidate and explicit mora CTC peak in Stage 3."""
    from soramimic_score import (
        Boundary,
        Evidence,
        LyricSpan,
        NoteCandidate,
        NoteRunConfig,
        ObservedSingingUnit,
        ReadingCandidate,
        VocalizationReattack,
        build_known_lyrics_document,
        compile_score,
    )

    if len(line_texts) != len(selected_readings) or not line_texts:
        raise ValueError("Stage 3には同数の歌詞行と読みが必要です")
    if type(enable_repeated_vocalization) is not bool:
        raise TypeError("反復音節補完の有効化指定はboolである必要があります")
    if enable_repeated_vocalization and whisper_line_windows is None:
        raise ValueError("反復音節補完には歌詞行ごとのWhisper区間が必要です")
    if enable_repeated_vocalization and ctc_emissions is None:
        raise ValueError("反復音節補完には未条件付けCTC出力が必要です")
    if (whisper_line_windows is not None
            and len(whisper_line_windows) != len(line_texts)):
        raise ValueError("Stage 3には歌詞行ごとのWhisper区間が必要です")
    for line, reading in enumerate(selected_readings):
        items = [item for item in aligned if item.line == line]
        if ([item.mora for item in items] != list(range(len(items)))
                or "".join(item.kana for item in items) != reading):
            raise ValueError("Stage 3へ渡すモーラ順が選択済み読みと一致しません")
    if any(item.line < 0 or item.line >= len(line_texts) for item in aligned):
        raise ValueError("Stage 3へ渡すモーラの歌詞行が不正です")
    if not melody_notes:
        raise ValueError("Stage 3にはSheetSageノート候補が必要です")

    canonical_text = "\n".join(line_texts)
    spans = []
    offset = 0
    for text, reading in zip(line_texts, selected_readings, strict=True):
        spans.append(LyricSpan(
            text,
            (offset, offset + len(text)),
            (ReadingCandidate(reading, "selected-reading", 1.0),),
        ))
        offset += len(text) + 1

    evidence = []
    observations = []
    for item in aligned:
        if (not math.isfinite(item.start_sec + item.end_sec + item.score)
                or item.start_sec < 0 or item.end_sec <= item.start_sec):
            raise ValueError("Stage 3へ渡すCTCモーラ時刻が不正です")
        confidence = max(0.0, min(1.0, float(item.score)))
        evidence_id = f"mora-ctc-{item.line}-{item.mora}"
        center = (item.start_sec + item.end_sec) / 2
        evidence.append(Evidence(
            evidence_id,
            "reazon-kana-ctc",
            "mora-ctc-anchor",
            confidence,
            {
                "time_sec": center,
                "start_sec": item.start_sec,
                "end_sec": item.end_sec,
                "conditioned_on_text": True,
            },
        ))
        observations.append(ObservedSingingUnit(
            (item.kana,),
            Boundary(item.start_sec, confidence, (evidence_id,)),
            None,
            Boundary(item.end_sec, confidence, (evidence_id,)),
            confidence, (evidence_id,),
        ))

    document = build_known_lyrics_document(
        canonical_text, tuple(spans), tuple(observations), tuple(evidence),
    )
    note_evidence = tuple(Evidence(
        f"sheetsage-note-{index}",
        "sheetsage2-vocal",
        "model-note",
        0.0,
        {"confidence_available": False},
    ) for index, _note in enumerate(melody_notes))
    candidates = tuple(NoteCandidate(
        f"sheetsage-{index}", note.start_sec, note.end_sec, note.midi_note,
        0.0, ("sheetsage2-vocal",), (note_evidence[index].id,),
    ) for index, note in enumerate(melody_notes))
    document = replace(
        document,
        evidence=document.evidence + note_evidence,
        note_candidates=candidates,
    )
    snapped_windows = (
        _snap_whisper_windows_to_sheetsage_rests(
            whisper_line_windows, melody_notes,
        )
        if whisper_line_windows is not None
        else None
    )
    reattacks_by_utterance: dict[str, tuple[VocalizationReattack, ...]] = {}
    if enable_repeated_vocalization:
        from .kana import split_moras
        from .mora_align import decode_repeated_mora_reattacks

        assert snapped_windows is not None
        assert ctc_emissions is not None
        for index, (reading, window) in enumerate(
            zip(selected_readings, snapped_windows, strict=True)
        ):
            if index in fixed_reading_indices:
                continue
            moras = split_moras(reading)
            if (len(moras) < 2 or len(set(moras)) != 1
                    or moras[0] in {"ン", "ッ", "ー"}):
                continue
            raw_events = decode_repeated_mora_reattacks(
                ctc_emissions, moras[0], *window,
            )
            if len(raw_events) < len(moras):
                continue
            reattacks_by_utterance[f"u{index}"] = tuple(
                VocalizationReattack(
                    event.start_sec,
                    event.end_sec,
                    event.confidence,
                    "reazon-kana-ctc-target-posterior",
                )
                for event in raw_events
            )
    score = compile_score(
        document,
        config=NoteRunConfig(
            whisper_boundary_cost_per_sec2=_WHISPER_BOUNDARY_COST_PER_SEC2,
        ),
        line_windows_by_utterance=(
            {f"u{index}": window
             for index, window in enumerate(snapped_windows)}
            if snapped_windows is not None
            else None
        ),
        vocalization_reattacks_by_utterance=(
            reattacks_by_utterance if enable_repeated_vocalization else None
        ),
    )
    return score.observations, score.score
