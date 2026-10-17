"""Align the lyrics and readings already chosen in an editor export."""

from __future__ import annotations

from collections.abc import Callable
from itertools import accumulate, pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from soramimic_score import ModelConfig, ScoreDocument


def editor_audio_lines(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Keep source phrase order, including empty rows, without selecting readings."""
    from soramimic_score import normalize_lyric_input, normalize_reading

    units = payload["unitsList"]
    candidates = [payload.get("originalLines"), payload.get("phrases")]
    if isinstance(payload.get("lyrics"), str):
        candidates.append(payload["lyrics"].splitlines())
    texts = next((rows for rows in candidates if isinstance(rows, list)
                  and len(rows) == len(units) and all(isinstance(s, str) for s in rows)), None)
    if texts is None:
        raise ValueError("歌詞JSONに、読みと行数が一致する元歌詞が必要です")
    tokens = payload.get("tokensList")
    if (isinstance(tokens, list) and len(tokens) == len(units)
            and all(isinstance(row, list) and all(isinstance(token, dict)
                    and isinstance(token.get("surface_form"), str) for token in row)
                    for row in tokens)):
        # Video exports can keep kana in phrases and original spelling in tokens.
        texts = ["".join(token["surface_form"] for token in row) or text
                 for row, text in zip(tokens, texts, strict=True)]
    result = []
    for index, (text, row) in enumerate(zip(texts, units, strict=True)):
        normalize_lyric_input(text)
        if not row and not text.strip():
            if payload["results"][index]:
                raise ValueError(f"歌詞JSONの{index + 1}行目: 空行に単語が割り当てられています")
            result.append((text, ""))
            continue
        if (not text.strip() or not row or any(not isinstance(unit, dict)
                or not isinstance(unit.get("pronunciation"), str)
                or not unit["pronunciation"] for unit in row)):
            raise ValueError(f"歌詞JSONの{index + 1}行目に元歌詞と読みが必要です")
        try:
            reading = normalize_reading("".join(unit["pronunciation"] for unit in row))
        except ValueError as exc:
            raise ValueError(f"歌詞JSONの{index + 1}行目の読みが不正です: {exc}") from exc
        result.append((text, reading))
    if not any(reading for _, reading in result):
        raise ValueError("歌詞JSONに読みのある歌詞が必要です")
    return result


def editor_word_boundaries(payload: dict[str, Any]) -> list[tuple[int, ...]]:
    """Project edited source-unit spans to mora boundaries, without rereading."""
    from soramimic_score import kana_to_moras, normalize_reading

    boundaries: list[tuple[int, ...]] = []
    for index, (units, words) in enumerate(zip(
        payload["unitsList"], payload["results"], strict=True,
    )):
        if not units:
            boundaries.append(())
            continue
        source = "".join(unit["pronunciation"] for unit in units)
        reading = normalize_reading(source)
        if len(source) != len(reading):
            raise ValueError(f"歌詞JSONの{index + 1}行目の単語境界を読みへ対応付けられません")
        moras = kana_to_moras(reading)
        mora_offsets = dict(zip(accumulate(map(len, moras), initial=0),
                                range(len(moras) + 1), strict=True))
        unit_offsets = list(accumulate((len(unit["pronunciation"]) for unit in units), initial=0))
        cuts = set()
        for word in words:
            start, end = word["period"]
            if not 0 <= start < end <= len(units):
                raise ValueError(f"歌詞JSONの{index + 1}行目の単語範囲が不正です")
            for unit_index in (start, end):
                offset = unit_offsets[unit_index]
                if offset not in mora_offsets:
                    raise ValueError(f"歌詞JSONの{index + 1}行目の単語境界がモーラの途中にあります")
                if 0 < mora_offsets[offset] < len(moras):
                    cuts.add(mora_offsets[offset])
        boundaries.append(tuple(sorted(cuts)))
    return boundaries


def align_editor_audio(
    path: Path, rows: list[tuple[str, str]], config: ModelConfig,
    on_progress: Callable[[str], None], accompaniment_path: Path | None,
    *, word_boundaries: list[tuple[int, ...]] | None = None,
) -> ScoreDocument:
    """Align fixed JSON readings across the whole audio without recognition."""
    from soramimic_score import (
        LyricLine,
        ReadingSelection,
        build_audio_observations,
        compile_score,
    )
    from soramimic_score.media import decoded_audio
    from soramimic_score.models import prepared_adapters

    active = [(index, text, kana) for index, (text, kana) in enumerate(rows) if kana]
    if word_boundaries is not None and len(word_boundaries) != len(rows):
        raise ValueError("歌詞JSONの単語境界と歌詞行数が一致しません")
    lines = tuple(LyricLine(text) for _, text, _ in active)
    readings = tuple(ReadingSelection(kana, "editor-json", 1., detail={
        "editor_line_index": index, "fixed": True,
        **({"word_boundaries": list(word_boundaries[index])}
           if word_boundaries is not None else {}),
    }) for index, _, kana in active)
    on_progress("音声を読み込んでいます")
    with decoded_audio(path) as source:
        if config.separate_vocals:
            on_progress("歌声を分離しています")
        with prepared_adapters(source, config, accompaniment_path=accompaniment_path) as adapters:
            # Untimed lines select Score's joint full-audio CTC alignment. JSON
            # row breaks retain lyric ownership without imposing time windows.
            on_progress("JSONの読みを音源に合わせています")
            moras = adapters.mora_aligner(source, lines, readings)
            on_progress("音符と音高を推定しています")
            notes = adapters.melody_transcriber(source)
            on_progress("楽譜データを組み立てています")
            observations = build_audio_observations(
                lines, readings, moras, notes, allow_empty_melody=True,
            )
            segments: dict[str, str] | None = None
            if word_boundaries is not None and any(word_boundaries):
                selected = {reading.id: reading for reading in observations.readings}
                segments = {}
                for (index, _, _), utterance in zip(active, observations.utterances, strict=True):
                    mora_ids = selected[utterance.selected_reading_id].mora_ids
                    cuts = (0, *word_boundaries[index], len(mora_ids))
                    for group, (start, end) in enumerate(pairwise(cuts)):
                        segments.update({mid: f"{utterance.id}:word-{group}"
                                         for mid in mora_ids[start:end]})
            result = (compile_score(observations, segment_ids_by_mora=segments)
                      if segments is not None else compile_score(observations))
            if adapters.vocal_activity is not None:
                from soramimic_score.spoken import add_spoken_fallback

                windows = {f"u{i}": (
                    min(m.start_sec for m in moras if m.line_index == i),
                    max(m.end_sec for m in moras if m.line_index == i),
                ) for i in range(len(lines))}
                result = add_spoken_fallback(
                    result, windows,
                    lambda bounds: adapters.vocal_activity(source, bounds),
                    fill_unpitched_lines=True,
                )
            return result
