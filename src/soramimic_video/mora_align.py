"""vocals.wav + 歌詞カナ → forced alignment でモーラごとの歌唱時刻を得る。

reazon-research/japanese-wav2vec2-base-rs35kh の CTC 出力に
torchaudio.functional.forced_align を適用する(方式は forced-alignment リポで検証済み)。
モーラをカナ1文字ずつのトークンに落としてアライメントし、
モーラの時刻 = 構成トークンのスパンの結合とする。

長い音源はチャンクに分けて logits を計算して連結する(CTCのフレーム独立性を利用)。
CTC のスパンはスパイク状で実際の歌唱区間より短いため、end_sec は後段
(analyze_audio)で有声区間に沿って伸長する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import jaconv

logger = logging.getLogger(__name__)

MODEL_NAME = "reazon-research/japanese-wav2vec2-base-rs35kh"
SAMPLING_RATE = 16000
FRAME_SAMPLES = 320  # wav2vec2のフレームストライド(20ms @ 16kHz)
_CHUNK_SAMPLES = 320 * 1000  # 20秒
_OVERLAP_SAMPLES = 320 * 100  # 2秒(フレーム境界に揃える)
_PAD_SEC = 0.5  # モデル推奨の前後パディング


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
                    logger.warning("語彙に無い文字を無視: %r (行%d)", ch, li)
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


def _compute_log_probs(vocals_path: Path, device: str):  # -> torch.Tensor (T, C)
    import librosa
    import numpy as np
    import torch
    from transformers import AutoProcessor, Wav2Vec2ForCTC

    logger.info("wav2vec2(%s)でCTC確率を計算中...", MODEL_NAME)
    model = Wav2Vec2ForCTC.from_pretrained(MODEL_NAME).to(device)
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


def align_moras(
    vocals_path: Path,
    line_moras: list[list[str]],
    device: str | None = None,
) -> list[AlignedMora]:
    """行ごとのモーラ列を音源にアライメントし、全モーラの時刻列を返す。

    戻り値は入力の (行, モーラ) と同順・同数。
    """
    try:
        import torch
        import torchaudio.functional as taf
        from transformers import Wav2Vec2CTCTokenizer
    except ImportError as e:
        raise RuntimeError(
            "torch/torchaudio/transformers がインストールされていません"
            "(uv sync --extra audio)"
        ) from e

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    tokenizer = Wav2Vec2CTCTokenizer.from_pretrained(MODEL_NAME)
    targets, owners = build_targets(line_moras, tokenizer.get_vocab())
    if not targets:
        raise ValueError("アライメント可能なカナがありません")

    log_probs = _compute_log_probs(vocals_path, device)
    logger.info("forced alignment実行中(%dトークン)...", len(targets))
    alignments, scores = taf.forced_align(
        log_probs.unsqueeze(0).to(torch.float32),
        torch.tensor([targets]),
        blank=0,
    )
    spans = taf.merge_tokens(alignments[0], scores[0].exp())
    if len(spans) != len(targets):
        raise RuntimeError(
            f"アライメント結果のトークン数が不一致: {len(spans)} != {len(targets)}"
        )

    # (行,モーラ) ごとにスパンを集約。得られなかったモーラは start_sec=-1 で後補間
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
