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
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .audio_project import DEFAULT_BPM, MoraNote, build_project, write_srt
from .kana import split_moras
from .project import Project
from .ruby import strip_ruby
from .semantic_lyrics import SemanticLyricDecision
from .transcribe import DEFAULT_WHISPER_MODEL, TranscribedLine

if TYPE_CHECKING:
    from .audio_melody import MelodyNote

logger = logging.getLogger(__name__)

ANALYZE_DIR = "analyze_audio"
SEPARATION_DIR = "separation"


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
    return any(
        len(options) > 1 and any(len(candidate) == len(options[0]) for candidate in options[1:])
        for options in variants
    )


def _filter_reading_evidence(
    evidence: dict[str, object], kept_line_indices: list[int]
) -> dict[str, object]:
    """Reindex KanaWhisper diagnostics after the semantic gate removes lines."""
    payload = json.loads(json.dumps(evidence))
    line_map = {old: new for new, old in enumerate(kept_line_indices)}
    lines = [payload["lines"][index] for index in kept_line_indices]
    used_contexts = sorted({
        item["context_index"] for item in lines if item["context_index"] is not None
    })
    context_map = {old: new for new, old in enumerate(used_contexts)}
    contexts = [payload["contexts"][index] for index in used_contexts]
    for new_index, item in enumerate(lines):
        item["line"] = new_index
        if item["context_index"] is not None:
            item["context_index"] = context_map[item["context_index"]]
    for context in contexts:
        context["line_indices"] = [
            line_map[index]
            for index in context["line_indices"]
            if index in line_map
        ]
    payload["lines"] = lines
    payload["contexts"] = contexts
    return payload


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
        align_moras_with_variants,
        retry_pathological_line_alignments,
    )
    from .reading import reading_candidates

    _require_audio_pipeline()
    emissions = None
    recognized_windows = None
    recognition_mode = None
    recognition_windows_fallback = False
    decisions: list[SemanticLyricDecision] = []
    recognition_lines: list[TranscribedLine] = []
    retained_indices: list[int] = []
    retained_lines: list[TranscribedLine] = []
    localized_recoveries: list[dict[str, object]] = []
    localized_alignment_retries: list[dict[str, object]] = []

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

    # 2. 歌詞行の決定
    if lyrics_path is not None:
        line_texts = [
            ln.strip() for ln in lyrics_path.read_text(encoding="utf-8").splitlines()
        ]
        line_texts = [ln for ln in line_texts if ln]
        logger.info("元歌詞: %d行 (%s)", len(line_texts), lyrics_path)
    else:
        from .semantic_lyrics import decide_recognized_line
        from .transcribe import transcribe_lines

        # Unknown lyrics use one complete, deterministic Whisper transcript.
        # CTC remains downstream for mora timing, not for deciding whether
        # recognized text or its deterministic first reading is accepted.
        lines = prefetched_lines if prefetched_lines is not None else transcribe_lines(
            audio_path, whisper_model, device or "auto", vad_filter=False,
            condition_on_previous_text=False,
        )
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
        recognition_lines = lines
        decisions = [decide_recognized_line(line, sheetsage_notes) for line in lines]
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
    line_variants = [
        [split_moras(kana) for kana in reading_candidates(text)] or [[]]
        for text in line_texts
    ]
    for text, variants in zip(line_texts, line_variants, strict=True):
        if not variants[0]:
            logger.warning("カナ読みが得られない行をスキップ: %r", text)
    line_texts = [strip_ruby(text) for text in line_texts]
    from .mora_align import compute_emissions

    emissions = emissions or compute_emissions(vocals, device)
    default_variants = [[variants[0]] for variants in line_variants]
    initial_alignment = None
    if recognized_windows is None:
        initial_alignment, _ = align_moras_with_variants(
            vocals,
            default_variants,
            device=device,
            emissions=emissions,
            phonetic_aliases=True,
            line_windows=None,
        )
    if recognized_windows is None:
        evidence_windows = _alignment_line_windows(initial_alignment, len(line_variants))
    else:
        evidence_windows = recognized_windows

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
        [variants[index]] for variants, index in zip(line_variants, chosen, strict=True)
    ]
    if initial_alignment is not None and not any(chosen):
        aligned = initial_alignment
    else:
        try:
            aligned, _fixed_choices = align_moras_with_variants(
                vocals,
                selected_variants,
                device=device,
                emissions=emissions,
                phonetic_aliases=True,
                line_windows=recognized_windows,
            )
        except ValueError as exc:
            if recognized_windows is None:
                raise
            recognition_windows_fallback = True
            fallback_windows = recognized_windows
            logger.warning(
                "Whisper行時刻をCTC整列に使えないため全体整列へ切替: %s", exc
            )
            recognized_windows = None
            aligned, _fixed_choices = align_moras_with_variants(
                vocals,
                selected_variants,
                device=device,
                emissions=emissions,
                phonetic_aliases=True,
                line_windows=None,
            )
            aligned, retries = retry_pathological_line_alignments(
                vocals,
                selected_variants,
                aligned,
                fallback_windows,
                device=device,
                emissions=emissions,
                phonetic_aliases=True,
            )
            localized_alignment_retries.extend(retries)

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
            line_texts = [line.text for line in retained_lines]
            recognized_windows = _recognized_line_windows(retained_lines)
            line_variants = [
                [split_moras(kana) for kana in reading_candidates(text)] or [[]]
                for text in line_texts
            ]
            line_texts = [strip_ruby(text) for text in line_texts]
            chosen = [0] * len(line_variants)
            reading_evidence = None
            if _has_kana_choice(line_variants):
                chosen, reading_evidence = _choose_readings_with_kana(
                    audio_path,
                    vocals,
                    line_texts,
                    line_variants,
                    recognized_windows,
                    device=device or "auto",
                    shared_inference=shared_inference,
                )
            selected_variants = [
                [variants[index]]
                for variants, index in zip(line_variants, chosen, strict=True)
            ]
            # The final retained-line alignment replaces the screening pass and
            # therefore owns the retry provenance recorded below.
            localized_alignment_retries = []
            try:
                aligned, _fixed_choices = align_moras_with_variants(
                    vocals,
                    selected_variants,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                    line_windows=recognized_windows,
                )
                # The final alignment, rather than the discarded screening pass,
                # determines the provenance recorded in analysis.json.
                recognition_windows_fallback = False
            except ValueError as exc:
                recognition_windows_fallback = True
                fallback_windows = recognized_windows
                assert fallback_windows is not None
                logger.warning(
                    "局所再認識後のWhisper行時刻をCTC整列に使えないため"
                    "全体整列へ切替: %s",
                    exc,
                )
                recognized_windows = None
                aligned, _fixed_choices = align_moras_with_variants(
                    vocals,
                    selected_variants,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                    line_windows=None,
                )
                aligned, retries = retry_pathological_line_alignments(
                    vocals,
                    selected_variants,
                    aligned,
                    fallback_windows,
                    device=device,
                    emissions=emissions,
                    phonetic_aliases=True,
                )
                localized_alignment_retries.extend(retries)

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
                    "schema_version": 4,
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
                            "reject-full-line-non-lyric-pattern-unless-melody-and-"
                            "ctc-median-support"
                        ),
                        "ctc_median_threshold": MIN_CTC_MEDIAN_SCORE,
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
                            }
                            for line, decision in zip(
                                recognition_lines, decisions, strict=True
                            )
                        ],
                        "localized_recoveries": localized_recoveries,
                        "localized_alignment_retries": localized_alignment_retries,
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
            "(非歌詞テンプレートを%d行除外、非旋律音声を%d行保持)",
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
        }
        for decision in (decisions if recognition_mode is not None else [])
        if decision.status != "accepted"
    ]
    limitations = []
    if recognition_mode is not None:
        limitations.append(
            "未知歌詞はWhisperによる推定です。recognition.jsonで認識結果を確認できます。"
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
                "schema_version": 4,
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
                "mora_count": len(raw_alignment),
                "sources": {},
                "limitations": limitations,
                "diagnostics": [
                    {"stage": "recognition", **flag} for flag in recognition_flags
                ] + [
                    {"stage": "mora-ctc-local-retry", **item}
                    for item in localized_alignment_retries
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
        document, layers = build_stage3_layers(
            line_texts, selected_readings, raw_alignment, sheetsage_notes,
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
