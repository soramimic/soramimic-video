"""Bridge local acoustic observations into wav-to-xf Stage 3."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wav_to_xf import IntermediateRepresentation, Realization

    from .audio_melody import MelodyNote
    from .mora_align import AlignedMora


def build_stage3_layers(
    line_texts: Sequence[str],
    selected_readings: Sequence[str],
    aligned: Sequence[AlignedMora],
    melody_notes: Sequence[MelodyNote],
) -> tuple[IntermediateRepresentation, Realization]:
    """Use every SheetSage candidate and explicit mora CTC peak in Stage 3."""
    from wav_to_xf import (
        Boundary,
        Evidence,
        LyricSpan,
        NoteCandidate,
        ObservedSingingUnit,
        ReadingCandidate,
        build_known_lyrics_document,
    )
    from wav_to_xf.pipeline import run_stage3_document

    if len(line_texts) != len(selected_readings) or not line_texts:
        raise ValueError("Stage 3には同数の歌詞行と読みが必要です")
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
    run = run_stage3_document(document)
    return run.document, run.realization
