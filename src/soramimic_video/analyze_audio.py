"""analyze-audio ステージ: 歌唱音源 → project.json。

XF MIDI の代わりに歌唱音源(wav/mp3)を入力の起点にする(issue #1)。

1. demucs でボーカル/伴奏に分離(伴奏は mix ステージでそのまま使う)
2. 正式歌詞があれば、その文字列を一切書き換えず forced alignment する
3. SheetSage2 のボーカルノートを主音高としてモーラへ対応づける
4. モデルノートの空白だけを RMVPE 主・FCPE 確認で補い、音高不定でも
   spoken として全モーラを残す。モデル未設定時は既存pYIN実経路を使う
5. 固定BPMの tick に換算して project.json を組み立てる

目視検証用に moras.srt / lines.srt も書き出す。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .audio_project import DEFAULT_BPM, MoraNote, build_project, write_srt
from .kana import split_moras
from .project import Project
from .ruby import strip_ruby
from .transcribe import DEFAULT_WHISPER_MODEL

logger = logging.getLogger(__name__)

ANALYZE_DIR = "analyze_audio"
SEPARATION_DIR = "separation"
_MAX_LAST_NOTE_SEC = 4.0  # 後続モーラが無いときの音符長の上限


def _require_evidence_pipeline() -> None:
    try:
        from wav_to_xf.cplus import from_cplus_assignments  # noqa: F401
        from wav_to_xf.pipeline import run_stage3_document  # noqa: F401
        from wav_to_xf.realization import compile_realization  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "evidence歌詞パイプラインにはwav-to-xfパッケージが必要です。"
            "利用可能なローカルチェックアウトを uv pip install <checkout> で追加してください。"
        ) from exc


def _record_stage3_fallback(project_dir: Path, detail: str) -> None:
    """Mark that the complete CTC project replaced an unusable Stage 3 refinement."""
    analysis_path = project_dir / ANALYZE_DIR / "analysis.json"
    analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))
    analysis_data["stage3_correspondence"] = False
    analysis_data["limitations"].append(detail)
    analysis_path.write_text(
        json.dumps(analysis_data, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def analyze_audio(
    audio_path: Path,
    project_dir: Path,
    lyrics_path: Path | None = None,
    melody_midi: Path | None = None,
    melody_channel: int | None = None,
    bpm: float = DEFAULT_BPM,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    skip_separation: bool = False,
    device: str | None = None,
    progress: Callable[[float], None] | None = None,
    lyric_pipeline: str | None = None,
) -> Project:
    from .mora_align import align_moras_with_variants
    from .pitch import extract_pitch, mora_midi_notes, voiced_end
    from .reading import reading_candidates

    lyric_pipeline = lyric_pipeline or os.environ.get("SORAMIMIC_LYRIC_PIPELINE", "cplus")
    if lyric_pipeline not in {"cplus", "evidence"}:
        raise ValueError("lyric_pipelineはcplusまたはevidenceです")
    use_evidence = lyric_pipeline == "evidence"
    if use_evidence:
        _require_evidence_pipeline()
    emissions = None
    recognized_variants = None
    recognized_windows = None
    recognition_mode = None

    last_progress = 0.0

    def report(value: float) -> None:
        nonlocal last_progress
        from . import runproc

        runproc.raise_if_cancelled()
        last_progress = max(last_progress, max(0.0, min(1.0, value)))
        if progress is not None:
            progress(last_progress)

    report(0.01)
    # 1. 音源分離
    accompaniment: Path | None = None
    if skip_separation:
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
    elif use_evidence:
        from .audio_activity import activity_overlap_seconds, detect_audio_activity
        from .transcribe import transcribe_lines

        # Unknown lyrics use one complete, deterministic Whisper transcript.
        # CTC remains downstream for mora timing, not for deciding whether
        # recognized text or its deterministic first reading is accepted.
        lines = transcribe_lines(
            audio_path,
            whisper_model,
            device or "auto",
            vad_filter=False,
        )
        activity = detect_audio_activity(vocals)
        discarded_silence = [
            line
            for line in lines
            if activity_overlap_seconds(line.start_sec, line.end_sec, activity) < 0.1
        ]
        lines = [line for line in lines if line not in discarded_silence]
        if not lines:
            raise RuntimeError("Whisperが歌詞を認識できませんでした")
        line_texts = [line.text for line in lines]
        recognized_windows = [(line.start_sec, line.end_sec) for line in lines]
        recognized_variants = []
        for line in lines:
            readings = reading_candidates(line.text)
            recognized_variants.append(
                [split_moras(readings[0])] if readings else [[]]
            )
        recognition_mode = "whisper-mix-silence-guard"
        out = project_dir / ANALYZE_DIR
        out.mkdir(parents=True, exist_ok=True)
        (out / "recognition.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "mode": recognition_mode,
                    "model": whisper_model,
                    "silence_guard": {
                        "source": "separated-vocals" if not skip_separation else "input-audio",
                        "discarded_segments": [
                            {
                                "start_sec": line.start_sec,
                                "end_sec": line.end_sec,
                                "surface": line.text,
                            }
                            for line in discarded_silence
                        ],
                    },
                    "segments": [
                        {
                            "start_sec": line.start_sec,
                            "end_sec": line.end_sec,
                            "surface": line.text,
                        }
                        for line in lines
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        logger.info(
            "Whisper mix/no-VADの%d行を元歌詞として採用 (無音区間の誤認識を%d行除外)",
            len(line_texts),
            len(discarded_silence),
        )
    else:
        from .transcribe import transcribe_lines

        line_texts = [seg.text for seg in transcribe_lines(vocals, whisper_model)]
        if not line_texts:
            raise RuntimeError("Whisperが歌詞を認識できませんでした")
        logger.info("Whisper認識結果を元歌詞として使用: %d行", len(line_texts))

    # 3. カナ化 + forced alignment。正式歌詞がある場合、Whisper/ASRを通さず
    # その文字列の読みだけを時刻へ対応づける。読み候補は文字列を書き換えず、
    # ルビ・辞書から得た発音候補の音響スコア選択に限る。
    # 元歌詞は青空文庫ルビ記法(｜表層《よみ》)で読みを指定できる。カナ化には記法つきの
    # 行を渡し、字幕・表示に使うテキスト(line_texts)は素テキストに直しておく。
    line_variants = recognized_variants or [
        [split_moras(kana) for kana in reading_candidates(text)] or [[]]
        for text in line_texts
    ]
    for text, variants in zip(line_texts, line_variants, strict=True):
        if not variants[0]:
            logger.warning("カナ読みが得られない行をスキップ: %r", text)
    line_texts = [strip_ruby(text) for text in line_texts]
    if use_evidence:
        import soundfile as sf

        from .mora_align import compute_emissions

        emissions = emissions or compute_emissions(vocals, device)
        audio_duration_sec = float(sf.info(vocals).duration)
        aligned, chosen = align_moras_with_variants(
            vocals, line_variants, device=device, emissions=emissions, phonetic_aliases=True,
            line_windows=recognized_windows,
        )
    else:
        aligned, chosen = align_moras_with_variants(vocals, line_variants, device=device)
    raw_alignment = [replace(mora) for mora in aligned]
    expected_moras = sum(
        len(line[choice])
        for line, choice in zip(line_variants, chosen, strict=True)
    )
    if len(aligned) != expected_moras:
        raise RuntimeError("正式歌詞のモーラをすべてアライメントできませんでした")
    n_ambiguous = sum(1 for v in line_variants if len(v) > 1)
    if n_ambiguous:
        logger.info(
            "読み候補が複数の行: %d行(うち%d行で第2候補以降を採用)",
            n_ambiguous, sum(1 for k in chosen if k != 0),
        )

    report(0.48)

    # 4. ピッチ + 音符終端の伸長。pYINは既存の実経路・合成用fallback。
    # (中央値はビブラート・しゃくりに引っ張られる。XF正解評価 81%→84%)
    track = extract_pitch(vocals)
    for i, m in enumerate(aligned):
        limit = (
            aligned[i + 1].start_sec
            if i + 1 < len(aligned)
            else m.end_sec + _MAX_LAST_NOTE_SEC
        )
        if use_evidence:
            limit = min(limit, audio_duration_sec)
            if recognized_windows is not None:
                limit = min(limit, recognized_windows[m.line][1])
        m.end_sec = max(m.end_sec, voiced_end(track, m.start_sec, limit))
        if use_evidence:
            m.end_sec = min(m.end_sec, audio_duration_sec)
            if recognized_windows is not None:
                m.end_sec = min(m.end_sec, recognized_windows[m.line][1])
    midi_notes = mora_midi_notes(track, [(m.start_sec, m.end_sec) for m in aligned])
    report(0.62)

    # 5. モーラ音符列の確定
    sheetsage_notes = None
    if melody_midi is not None:
        # メロディMIDIがあればピッチ・タイミングを楽譜に寄せる(issue #3)。
        # f0由来のmidi_notesは余りモーラのフォールバックと移調補正に使う
        from .melody_align import apply_melody_midi

        mora_notes = apply_melody_midi(
            audio_path, melody_midi, melody_channel, aligned, midi_notes
        )
    else:
        from .audio_melody import (
            assign_mora_pitches,
            configured_capabilities,
            extract_fcpe,
            extract_rmvpe,
            transcribe_sheetsage,
        )

        actual_device = device
        if actual_device is None:
            try:
                import torch

                actual_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                actual_device = "cpu"
        raw_dir = project_dir / ANALYZE_DIR / "sheetsage-work"
        try:
            sheetsage_notes = transcribe_sheetsage(
                audio_path,
                raw_dir,
                device=actual_device,
                on_progress=lambda value: report(0.62 + value * 0.18),
            )
        finally:
            # Never retain detailed model output after success, failure, or cancel.
            shutil.rmtree(raw_dir, ignore_errors=True)
        if use_evidence and sheetsage_notes is None:
            raise RuntimeError(
                "evidence歌詞パイプラインにはSheetSage2モデル設定が必要です"
            )
        capabilities = configured_capabilities()
        rmvpe = fcpe = None
        if sheetsage_notes is not None and capabilities["rmvpe"] and capabilities["fcpe"]:
            logger.info("SheetSage2空白をRMVPE主・FCPE確認で検査中")
            rmvpe = extract_rmvpe(vocals, device=actual_device)
            report(0.86)
            fcpe = extract_fcpe(vocals, device=actual_device)
            report(0.94)
        if sheetsage_notes is None:
            # モデル未配置でも従来のpYIN経路は実際に動く。高度な由来を装わず、
            # analysis.json とUIで pyin_only と明示する。
            pitches: list[tuple[int, str, float | None]] = [
                (note, "recovered_note", None) for note in midi_notes
            ]
            mode = "pyin_only"
        else:
            assigned = assign_mora_pitches(
                aligned,
                sheetsage_notes,
                rmvpe=rmvpe,
                fcpe=fcpe,
                fallback_midi=midi_notes,
            )
            pitches = [(p.midi_note, p.source, p.confidence) for p in assigned]
            mode = (
                "sheetsage2_rmvpe_fcpe"
                if rmvpe is not None and fcpe is not None
                else "sheetsage2_only"
            )
        mora_notes = [
            MoraNote(
                line=m.line,
                kana=m.kana,
                start_sec=m.start_sec,
                end_sec=m.end_sec,
                midi_note=note,
                source=source,
                pitch_confidence=confidence,
            )
            for m, (note, source, confidence) in zip(aligned, pitches, strict=True)
        ]
        # SheetSageの詳細出力には入力由来の時刻列が含まれる。必要な由来集計だけを
        # projectへ残し、ジョブ内の一時推論出力は成功時にも保持しない。
        out = project_dir / ANALYZE_DIR
        out.mkdir(parents=True, exist_ok=True)
        counts = {
            source: sum(1 for note in mora_notes if note.source == source)
            for source in ("sheetsage_note", "recovered_note", "spoken")
        }
        limitations = []
        if mode != "sheetsage2_rmvpe_fcpe":
            limitations.append(
                "SheetSage2/RMVPE/FCPEの完全構成ではありません。"
                "音高推定結果はタイミングエディタで確認してください。"
            )
        if counts["spoken"]:
            limitations.append(
                f"音高不定の{counts['spoken']}モーラをspokenとして保持しました。"
            )
        if recognition_mode is not None:
            limitations.append(
                "未知歌詞はWhisperによる推定です。recognition.jsonで認識結果を確認できます。"
            )
        (out / "analysis.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": mode,
                    "official_lyrics": lyrics_path is not None,
                    "asr_used": lyrics_path is None,
                    "lyric_pipeline": lyric_pipeline,
                    "stage3_correspondence": use_evidence,
                    "recognition_mode": recognition_mode,
                    "recognition_coverage": None,
                    "recognition_flags": [],
                    "mora_count": len(mora_notes),
                    "sources": counts,
                    "limitations": limitations,
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
    if use_evidence:
        from .lyric_layers import apply_lyric_layers

        if len(project.notes) != len(raw_alignment):
            raise RuntimeError("全モーラをStage 3歌詞レイヤーへ引き渡せませんでした")
        selected_readings = [
            "".join(variants[index])
            for variants, index in zip(line_variants, chosen, strict=True)
        ]
        if sheetsage_notes is None:
            raise RuntimeError("Stage 3へ渡すSheetSage2ノート候補がありません")
        from .stage3 import build_stage3_layers

        out = project_dir / ANALYZE_DIR
        out.mkdir(parents=True, exist_ok=True)
        layers = None
        try:
            document, layers = build_stage3_layers(
                line_texts, selected_readings, raw_alignment, sheetsage_notes,
            )
        except ValueError as exc:
            # Stage 3 is a refinement of the complete CTC/pitch project built
            # above.  A malformed derived slot must not discard valid lyrics or
            # turn an otherwise renderable upload into a failed job.
            logger.warning(
                "Stage 3の合成計画を適用できないため、"
                "全モーラを保持したCTC整列結果へフォールバックします: %s",
                exc,
            )
            _record_stage3_fallback(
                project_dir,
                "Stage 3の合成計画を適用できなかったため、"
                "全モーラを保持したCTC整列結果を使用しました。",
            )
        else:
            (out / "correspondence.json").write_text(
                document.to_json(), encoding="utf-8"
            )
        if layers is not None and layers.unresolved_unit_ids:
            # Stage 3 may have no SheetSage candidate in a short or densely sung
            # lyric line.  The preceding CTC/pitch path already produced one
            # ordered, non-dropping note per official-lyrics mora, so retain that
            # complete project instead of turning an optional refinement into a
            # fatal generation error.
            logger.warning(
                "Stage 3で%dモーラのノートが未解決のため、"
                "全モーラを保持したCTC整列結果へフォールバックします",
                len(layers.unresolved_unit_ids),
            )
            _record_stage3_fallback(
                project_dir,
                "Stage 3で未解決のモーラがあったため、"
                "全モーラを保持したCTC整列結果を使用しました。",
            )
        elif layers is not None:
            apply_lyric_layers(project, layers.to_dict())

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
