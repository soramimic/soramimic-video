"""vocals.wav + 歌詞カナ → forced alignment でモーラごとの歌唱時刻を得る。

reazon-research/japanese-wav2vec2-base-rs35kh の CTC 出力に
torchaudio.functional.forced_align を適用する(方式は forced-alignment リポで検証済み)。
モーラをカナ1文字ずつのトークンに落としてアライメントし、
モーラの時刻 = 構成トークンのスパンの結合とする。

長い音源はチャンクに分けて logits を計算して連結する(CTCのフレーム独立性を利用)。
CTC のスパンはスパイク状で実際の歌唱区間より短いため、end_sec は後段
(analyze_audio)で有声区間に沿って伸長する。

行の読みに複数候補があるとき(align_moras_with_variants)は、初回アライメントで
行の時間範囲を出し、その範囲の log_probs に候補ごとの forced_align を掛けて
尤度の高い読みを選ぶ(音源による読みの曖昧性解消)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jaconv

logger = logging.getLogger(__name__)

MODEL_NAME = "reazon-research/japanese-wav2vec2-base-rs35kh"
SAMPLING_RATE = 16000
FRAME_SAMPLES = 320  # wav2vec2のフレームストライド(20ms @ 16kHz)
_CHUNK_SAMPLES = 320 * 1000  # 20秒
_OVERLAP_SAMPLES = 320 * 100  # 2秒(フレーム境界に揃える)
_PAD_SEC = 0.5  # モデル推奨の前後パディング
_VARIANT_MARGIN_FRAMES = 25  # 読み候補スコアリング時に行の前後へ付ける余白(0.5秒)
# 既定候補(yomi)を覆すのに要求する合計対数尤度差(対数ベイズ因子)。
# 平均だと行の長さで差が薄まる(違いは1-2モーラでも行全体で平均される)ため合計を使う。
# 実測: 正しい修正(アス,ヒガ)は約4-13、誤修正の例(ドッテ)は約1.2だった
_VARIANT_SCORE_MARGIN = 2.0


@dataclass
class AlignedMora:
    line: int
    mora: int  # 行内のモーラ番号
    kana: str
    start_sec: float
    end_sec: float
    score: float


def build_targets(
    line_moras: list[list[str]], char_to_id: dict[str, int]
) -> tuple[list[int], list[tuple[int, int]]]:
    """モーラ列をトークンID列にする。

    戻り値: (target_ids, owners)。owners[k] は target_ids[k] が属する
    (行番号, 行内モーラ番号)。語彙に無い文字は無視する(そのモーラの時刻は
    後で近傍から補間する)。
    """
    targets: list[int] = []
    owners: list[tuple[int, int]] = []
    for li, moras in enumerate(line_moras):
        for mi, mora in enumerate(moras):
            for ch in mora:
                tid = char_to_id.get(ch)
                if tid is None:
                    tid = char_to_id.get(jaconv.kata2hira(ch))
                if tid is None:
                    logger.debug("語彙に無い文字を無視: %r (行%d)", ch, li)
                    continue
                targets.append(tid)
                owners.append((li, mi))
    return targets, owners


def interpolate_missing(moras: list[AlignedMora]) -> None:
    """スパンが得られなかったモーラ(start_sec<0)を近傍から補間する(インプレース)。"""
    for i, m in enumerate(moras):
        if m.start_sec >= 0:
            continue
        prev_end = next(
            (moras[j].end_sec for j in range(i - 1, -1, -1) if moras[j].start_sec >= 0),
            0.0,
        )
        next_start = next(
            (moras[j].start_sec for j in range(i + 1, len(moras)) if moras[j].start_sec >= 0),
            prev_end,
        )
        m.start_sec = prev_end
        m.end_sec = max(next_start, prev_end)


def _compute_log_probs(vocals_path: Path, device: str) -> Any:  # torch.Tensor (T, C)
    import librosa
    import numpy as np
    import torch
    from transformers import AutoProcessor, Wav2Vec2ForCTC

    logger.info("wav2vec2(%s)でCTC確率を計算中...", MODEL_NAME)
    model = Wav2Vec2ForCTC.from_pretrained(MODEL_NAME).to(device)  # type: ignore[arg-type]
    processor = AutoProcessor.from_pretrained(MODEL_NAME)

    audio, _ = librosa.load(str(vocals_path), sr=SAMPLING_RATE, mono=True)
    audio = np.pad(audio, pad_width=int(_PAD_SEC * SAMPLING_RATE))

    chunks: list[torch.Tensor] = []
    pos = 0
    n = len(audio)
    while pos < n:
        s0 = max(0, pos - _OVERLAP_SAMPLES)
        s1 = min(n, pos + _CHUNK_SAMPLES + _OVERLAP_SAMPLES)
        input_values = processor(
            audio[s0:s1], return_tensors="pt", sampling_rate=SAMPLING_RATE
        ).input_values.to(device)
        with torch.inference_mode():
            logits = model(input_values).logits.cpu()[0]  # (T, C)
        # フレームiの開始サンプルは s0 + i*FRAME_SAMPLES。[pos, pos+chunk) 分だけ残す
        keep_from = (pos - s0) // FRAME_SAMPLES
        keep_to = logits.shape[0] if s1 >= n else keep_from + _CHUNK_SAMPLES // FRAME_SAMPLES
        chunks.append(logits[keep_from:keep_to])
        pos += _CHUNK_SAMPLES
    log_probs = torch.nn.functional.log_softmax(torch.cat(chunks), dim=-1)
    logger.debug("logits: %d frames x %d tokens", *log_probs.shape)
    return log_probs


def _forced_align(log_probs: Any, targets: list[int]) -> list[Any]:
    """forced_align + merge_tokens。戻り値はTokenSpan列(フレーム単位)。"""
    import torch
    import torchaudio.functional as taf

    alignments, scores = taf.forced_align(
        log_probs.unsqueeze(0).to(torch.float32),
        torch.tensor([targets]),
        blank=0,
    )
    return taf.merge_tokens(alignments[0], scores[0].exp())


def _variant_score(log_probs_slice: Any, targets: list[int]) -> float:
    """行の区間log_probsに対する読み候補の合計対数尤度。

    候補間の差が対数ベイズ因子になる(同じ区間・同じフレーム数で比較するため)。
    """
    import torch
    import torchaudio.functional as taf

    if not targets or log_probs_slice.shape[0] < len(targets):
        return float("-inf")
    try:
        _, scores = taf.forced_align(
            log_probs_slice.unsqueeze(0).to(torch.float32),
            torch.tensor([targets]),
            blank=0,
        )
    except RuntimeError:
        return float("-inf")
    return float(scores[0].sum())


def _spans_to_moras(
    spans: list[Any],
    owners: list[tuple[int, int]],
    line_moras: list[list[str]],
) -> list[AlignedMora]:
    """トークンスパンを(行,モーラ)ごとに集約してAlignedMora列にする。"""
    moras = [
        AlignedMora(line=li, mora=mi, kana=kana, start_sec=-1.0, end_sec=-1.0, score=0.0)
        for li, line in enumerate(line_moras)
        for mi, kana in enumerate(line)
    ]
    index = {(m.line, m.mora): m for m in moras}
    for span, owner in zip(spans, owners, strict=True):
        start = max(0.0, span.start * FRAME_SAMPLES / SAMPLING_RATE - _PAD_SEC)
        end = max(0.0, span.end * FRAME_SAMPLES / SAMPLING_RATE - _PAD_SEC)
        m = index[owner]
        if m.start_sec < 0:
            m.start_sec = start
            m.score = span.score
        m.end_sec = end
    interpolate_missing(moras)
    return moras


def align_moras_with_variants(
    vocals_path: Path,
    line_variants: list[list[list[str]]],
    device: str | None = None,
) -> tuple[list[AlignedMora], list[int]]:
    """行ごとの読み候補つきアライメント。

    line_variants[行] = 候補読みのモーラ列のリスト(先頭が既定)。
    候補が複数の行は、初回アライメントで得た行の時間範囲のlog_probsに
    候補ごとのforced_alignを掛け、尤度の高い読みを採用して最終アライメントする。

    戻り値: (全モーラの時刻列, 行ごとの採用候補index)
    """
    try:
        import torch
    except ImportError as e:
        raise RuntimeError(
            "torch/torchaudio/transformers がインストールされていません"
            "(uv sync --extra audio)"
        ) from e
    from transformers import Wav2Vec2CTCTokenizer

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    vocab = Wav2Vec2CTCTokenizer.from_pretrained(MODEL_NAME).get_vocab()
    log_probs = _compute_log_probs(vocals_path, device)

    chosen = [0] * len(line_variants)
    line_moras = [variants[0] for variants in line_variants]
    targets, owners = build_targets(line_moras, vocab)
    if not targets:
        raise ValueError("アライメント可能なカナがありません")
    logger.info("forced alignment実行中(%dトークン)...", len(targets))
    spans = _forced_align(log_probs, targets)
    if len(spans) != len(targets):
        raise RuntimeError(
            f"アライメント結果のトークン数が不一致: {len(spans)} != {len(targets)}"
        )

    # 読み候補が複数の行を音響スコアで判定
    ambiguous = [li for li, v in enumerate(line_variants) if len(v) > 1]
    if ambiguous:
        line_frames: dict[int, tuple[int, int]] = {}
        for span, (li, _mi) in zip(spans, owners, strict=True):
            f0, f1 = line_frames.get(li, (span.start, span.end))
            line_frames[li] = (min(f0, span.start), max(f1, span.end))
        changed = False
        for li in ambiguous:
            if li not in line_frames:
                continue
            f0, f1 = line_frames[li]
            lo = max(0, f0 - _VARIANT_MARGIN_FRAMES)
            hi = min(log_probs.shape[0], f1 + _VARIANT_MARGIN_FRAMES)
            scores = []
            for k, cand in enumerate(line_variants[li]):
                cand_targets, _ = build_targets([cand], vocab)
                s = _variant_score(log_probs[lo:hi], cand_targets)
                logger.debug("行%d 候補%d %r: score=%.3f", li, k, "".join(cand), s)
                scores.append(s)
            best_k = max(range(len(scores)), key=lambda k: scores[k])
            if best_k != 0 and scores[best_k] < scores[0] + _VARIANT_SCORE_MARGIN:
                best_k = 0  # 僅差なら既定候補(yomi)を維持
            if best_k != 0:
                logger.info(
                    "行%d: 音響スコアで読み候補%dを採用 (%r -> %r)",
                    li, best_k,
                    "".join(line_variants[li][0]), "".join(line_variants[li][best_k]),
                )
                chosen[li] = best_k
                changed = True
        if changed:
            line_moras = [v[k] for v, k in zip(line_variants, chosen, strict=True)]
            targets, owners = build_targets(line_moras, vocab)
            spans = _forced_align(log_probs, targets)
            if len(spans) != len(targets):
                raise RuntimeError("再アライメントのトークン数が不一致")

    return _spans_to_moras(spans, owners, line_moras), chosen


def align_moras(
    vocals_path: Path,
    line_moras: list[list[str]],
    device: str | None = None,
) -> list[AlignedMora]:
    """行ごとのモーラ列を音源にアライメントし、全モーラの時刻列を返す。

    戻り値は入力の (行, モーラ) と同順・同数。
    """
    moras, _ = align_moras_with_variants(
        vocals_path, [[lm] for lm in line_moras], device=device
    )
    return moras
