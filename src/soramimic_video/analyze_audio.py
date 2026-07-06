"""analyze-audio ステージ: 歌唱音源 → project.json。

XF MIDI の代わりに歌唱音源(wav/mp3)を入力の起点にする(issue #1)。

1. demucs でボーカル/伴奏に分離(伴奏は mix ステージでそのまま使う)
2. 歌詞テキストが無ければ Whisper の認識結果を元歌詞にする
3. 歌詞をカナ化し wav2vec2 CTC forced alignment でモーラ時刻を得る
4. pyin の f0 からモーラごとの midi_note を決め、有声区間に沿って
   音符終端を伸長する(CTCスパンはスパイク状で短いため)
5. 固定BPMの tick に換算して project.json を組み立てる

目視検証用に moras.srt / lines.srt も書き出す。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .audio_project import DEFAULT_BPM, MoraNote, build_project, write_srt
from .kana import split_moras
from .project import Project
from .transcribe import DEFAULT_WHISPER_MODEL

logger = logging.getLogger(__name__)

ANALYZE_DIR = "analyze_audio"
SEPARATION_DIR = "separation"
_MAX_LAST_NOTE_SEC = 4.0  # 後続モーラが無いときの音符長の上限


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
) -> Project:
    from .mora_align import align_moras_with_variants
    from .pitch import extract_pitch, mora_midi_notes, voiced_end
    from .reading import reading_candidates

    # 1. 音源分離
    accompaniment: Path | None = None
    if skip_separation:
        vocals = audio_path
        logger.info("音源分離をスキップ(入力をそのままボーカルとして扱います)")
    else:
        from .separation import separate

        vocals, accompaniment = separate(audio_path, project_dir / SEPARATION_DIR)

    # 2. 歌詞行の決定
    if lyrics_path is not None:
        line_texts = [
            ln.strip() for ln in lyrics_path.read_text(encoding="utf-8").splitlines()
        ]
        line_texts = [ln for ln in line_texts if ln]
        logger.info("元歌詞: %d行 (%s)", len(line_texts), lyrics_path)
    else:
        from .transcribe import transcribe_lines

        line_texts = [seg.text for seg in transcribe_lines(vocals, whisper_model)]
        if not line_texts:
            raise RuntimeError("Whisperが歌詞を認識できませんでした")
        logger.info("Whisper認識結果を元歌詞として使用: %d行", len(line_texts))

    # 3. カナ化 + forced alignment。読み候補が割れた行は音響スコアで判定する
    line_variants = [
        [split_moras(kana) for kana in reading_candidates(text)] or [[]]
        for text in line_texts
    ]
    for text, variants in zip(line_texts, line_variants, strict=True):
        if not variants[0]:
            logger.warning("カナ読みが得られない行をスキップ: %r", text)
    aligned, chosen = align_moras_with_variants(vocals, line_variants, device=device)
    n_ambiguous = sum(1 for v in line_variants if len(v) > 1)
    if n_ambiguous:
        logger.info(
            "読み候補が複数の行: %d行(うち%d行で第2候補以降を採用)",
            n_ambiguous, sum(1 for k in chosen if k != 0),
        )

    # 4. ピッチ + 音符終端の伸長
    track = extract_pitch(vocals)
    for i, m in enumerate(aligned):
        limit = (
            aligned[i + 1].start_sec
            if i + 1 < len(aligned)
            else m.end_sec + _MAX_LAST_NOTE_SEC
        )
        m.end_sec = max(m.end_sec, voiced_end(track, m.start_sec, limit))
    midi_notes = mora_midi_notes(track, [(m.start_sec, m.end_sec) for m in aligned])

    # 5. モーラ音符列の確定
    if melody_midi is not None:
        # メロディMIDIがあればピッチ・タイミングを楽譜に寄せる(issue #3)。
        # f0由来のmidi_notesは余りモーラのフォールバックと移調補正に使う
        from .melody_align import apply_melody_midi

        mora_notes = apply_melody_midi(
            audio_path, melody_midi, melody_channel, aligned, midi_notes
        )
    else:
        mora_notes = [
            MoraNote(
                line=m.line,
                kana=m.kana,
                start_sec=m.start_sec,
                end_sec=m.end_sec,
                midi_note=note,
            )
            for m, note in zip(aligned, midi_notes, strict=True)
        ]
    project = build_project(
        audio_path=audio_path,
        vocals_path=None if skip_separation else vocals,
        accompaniment_path=accompaniment,
        line_texts=line_texts,
        mora_notes=mora_notes,
        bpm=bpm,
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
    return project
