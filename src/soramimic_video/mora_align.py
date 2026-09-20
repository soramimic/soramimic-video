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
import math
from dataclasses import dataclass, replace
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
# A CTC target can carry clear posterior spikes without ever beating blank.  This
# is common for sung non-lexical syllables.  Keep only physically separate peaks
# with both absolute and within-window prominence; neither rule limits the count.
_REATTACK_MIN_POSTERIOR_PROMINENCE = 0.01
_REATTACK_RELATIVE_PROMINENCE = 0.03
_REATTACK_MIN_DISTANCE_FRAMES = 3
# Whisper emits adjacent decimal timestamps that can differ by a few ULPs after
# JSON/Python round trips.  One microsecond is still far below a CTC frame (20 ms).
_WINDOW_BOUNDARY_EPSILON_SEC = 1e-6
# CTC evidence is spike-like.  These conservative physical guards detect a line
# assigned to a distant repeated phrase without treating an early ASR boundary as
# wrong (Whisper can legitimately begin after the first sung mora).
PATHOLOGICAL_MORA_SPAN_SEC = 1.2
PATHOLOGICAL_WINDOW_START_UNDERRUN_SEC = 0.75
PATHOLOGICAL_WINDOW_END_OVERRUN_SEC = 0.75
_MODEL_CACHE: dict[str, tuple[Any, Any]] = {}


def _frame_ceiling(time_sec: float) -> int:
    """Map padded seconds to a half-open CTC frame boundary.

    Decimal timestamps that are exactly on the 20 ms grid can land a few ULPs
    above an integer after floating-point addition and division.  A raw ceil
    would then include one frame beginning at the window's exclusive end.
    """
    frame_sec = FRAME_SAMPLES / SAMPLING_RATE
    return math.ceil((time_sec + _PAD_SEC) / frame_sec - 1e-9)


@dataclass
class AlignedMora:
    line: int
    mora: int  # 行内のモーラ番号
    kana: str
    start_sec: float
    end_sec: float
    score: float


@dataclass
class CTCEmissions:
    """Shared model output; timing scores are not calibrated onset probabilities."""

    log_probs: Any
    vocab: dict[str, int]


@dataclass(frozen=True)
class KanaCTCEvent:
    """One unconditioned greedy CTC token event in absolute audio time."""

    kana: str
    start_sec: float
    end_sec: float
    confidence: float


class CTCWindowCapacityError(RuntimeError):
    """A transcript cannot fit into its fixed CTC frame window."""

    def __init__(
        self,
        *,
        available_frames: int,
        target_count: int,
        adjacent_repeats: int,
        line: int | None = None,
    ) -> None:
        self.line = line
        self.available_frames = available_frames
        self.target_count = target_count
        self.adjacent_repeats = adjacent_repeats
        self.required_frames = target_count + adjacent_repeats
        super().__init__(
            "CTC window has insufficient capacity: "
            f"{available_frames} frames for {target_count} targets "
            f"and {adjacent_repeats} adjacent repeats"
        )


def _require_ctc_capacity(
    available_frames: int, targets: list[int], *, line: int | None = None,
) -> None:
    adjacent_repeats = sum(
        left == right for left, right in zip(targets, targets[1:], strict=False)
    )
    if available_frames < len(targets) + adjacent_repeats:
        raise CTCWindowCapacityError(
            line=line,
            available_frames=available_frames,
            target_count=len(targets),
            adjacent_repeats=adjacent_repeats,
        )


def _validated_line_windows(
    line_windows: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Validate windows while folding floating-point dust into a shared boundary."""
    normalized: list[tuple[float, float]] = []
    previous_end = 0.0
    for raw_start, end in line_windows:
        start = raw_start
        if not math.isfinite(start + end):
            raise ValueError("line_windows must be finite, positive, and nonoverlapping")
        if start < previous_end:
            if previous_end - start > _WINDOW_BOUNDARY_EPSILON_SEC:
                raise ValueError("line_windows must be finite, positive, and nonoverlapping")
            start = previous_end
        if not 0 <= previous_end <= start < end:
            raise ValueError("line_windows must be finite, positive, and nonoverlapping")
        normalized.append((start, end))
        previous_end = end
    return normalized


def compute_emissions(vocals_path: Path, device: str | None = None) -> CTCEmissions:
    import torch
    from transformers import Wav2Vec2CTCTokenizer

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    vocab = Wav2Vec2CTCTokenizer.from_pretrained(MODEL_NAME).get_vocab()
    return CTCEmissions(_compute_log_probs(vocals_path, device), vocab)


def decode_kana_window(
    emissions: CTCEmissions, start_sec: float, end_sec: float,
) -> tuple[str, float]:
    """Greedy decode an audio window without any transcript or forced targets.

    The returned score is the geometric mean emitted-token posterior, not a
    calibrated transcript probability. Blank frames still belong to the analyzed
    window, but are neither kana nor evidence of a vowel's duration.
    """
    if not 0 <= start_sec < end_sec:
        raise ValueError("CTC window must have positive duration")
    # A half-open partition assigns a boundary frame to exactly one adjacent
    # window. floor(start)/ceil(end) would decode that frame twice.
    first = max(0, _frame_ceiling(start_sec))
    last = min(len(emissions.log_probs), _frame_ceiling(end_sec))
    values = emissions.log_probs[first:last]
    if len(values) == 0:
        return "", 0.0
    # numpy makes this pure decode testable without model/runtime dependencies.
    import numpy as np

    matrix = np.asarray(values)
    best = matrix.argmax(axis=-1)
    vocabulary = {value: key for key, value in emissions.vocab.items()}
    chars: list[str] = []
    scores: list[float] = []
    previous = -1
    for index, token_id in enumerate(best):
        token_id = int(token_id)
        if token_id == 0 or token_id == previous:
            previous = token_id
            continue
        previous = token_id
        token = jaconv.hira2kata(vocabulary.get(token_id, ""))
        if not token or not all("ァ" <= ch <= "ヺ" or ch == "ー" for ch in token):
            continue
        # A crop can start inside a syllable; do not invent its missing head.
        if not chars and token[0] in "ァィゥェォャュョヮー":
            continue
        chars.append(token)
        scores.append(float(matrix[index, token_id]))
    return "".join(chars), math.exp(sum(scores) / len(scores)) if scores else 0.0


def decode_kana_events_window(
    emissions: CTCEmissions, start_sec: float, end_sec: float,
) -> tuple[KanaCTCEvent, ...]:
    """Return timed greedy kana events without rerunning the acoustic model."""
    if not 0 <= start_sec < end_sec:
        raise ValueError("CTC window must have positive duration")
    first = max(0, _frame_ceiling(start_sec))
    last = min(len(emissions.log_probs), _frame_ceiling(end_sec))
    values = collapse_kana_aliases(emissions)[first:last]
    if len(values) == 0:
        return ()

    import numpy as np

    matrix = np.asarray(values)
    best = matrix.argmax(axis=-1)
    vocabulary = {value: key for key, value in emissions.vocab.items()}
    frame_sec = FRAME_SAMPLES / SAMPLING_RATE
    output: list[KanaCTCEvent] = []
    index = 0
    while index < len(best):
        token_id = int(best[index])
        stop = index + 1
        while stop < len(best) and int(best[stop]) == token_id:
            stop += 1
        if token_id != 0:
            token = jaconv.hira2kata(vocabulary.get(token_id, ""))
            if token and all("ァ" <= char <= "ヺ" or char == "ー" for char in token):
                peak = max(float(matrix[frame, token_id]) for frame in range(index, stop))
                event_start = max(start_sec, (first + index) * frame_sec - _PAD_SEC)
                event_end = min(end_sec, (first + stop) * frame_sec - _PAD_SEC)
                if event_end > event_start:
                    output.append(KanaCTCEvent(
                        token, event_start, event_end, math.exp(peak),
                    ))
        index = stop
    return tuple(output)


def decode_repeated_mora_reattacks(
    emissions: CTCEmissions, mora: str, start_sec: float, end_sec: float,
) -> tuple[KanaCTCEvent, ...]:
    """Find posterior spikes for one Whisper-supplied mora without a count cap.

    Greedy CTC often emits blank throughout non-lexical singing even while the
    requested kana has distinct posterior spikes.  Read those acoustic spikes
    directly instead of requiring the kana to win the framewise argmax.
    """
    if not 0 <= start_sec < end_sec:
        raise ValueError("CTC window must have positive duration")
    target = tuple(jaconv.hira2kata(mora))
    if not target:
        return ()
    first = max(0, _frame_ceiling(start_sec))
    last = min(len(emissions.log_probs), _frame_ceiling(end_sec))
    if first >= last:
        return ()

    import numpy as np
    matrix = np.asarray(collapse_kana_aliases(emissions))[first:last]
    frame_sec = FRAME_SAMPLES / SAMPLING_RATE
    events: list[KanaCTCEvent] = []
    for kana in set(target):
        token_id = emissions.vocab.get(kana)
        if token_id is None:
            token_id = emissions.vocab.get(jaconv.kata2hira(kana))
        if token_id is None:
            continue
        posterior = np.exp(matrix[:, token_id])
        if not len(posterior):
            continue
        prominence = max(
            _REATTACK_MIN_POSTERIOR_PROMINENCE,
            float(posterior.max()) * _REATTACK_RELATIVE_PROMINENCE,
        )
        for index in _prominent_peak_indices(
            posterior, prominence, _REATTACK_MIN_DISTANCE_FRAMES,
        ):
            event_start = max(start_sec, (first + index) * frame_sec - _PAD_SEC)
            event_end = min(end_sec, event_start + frame_sec)
            if event_end > event_start:
                events.append(KanaCTCEvent(
                    kana, event_start, event_end, float(posterior[index]),
                ))
    events.sort(key=lambda event: (event.start_sec, event.end_sec, event.kana))
    result: list[KanaCTCEvent] = []
    index = 0
    while index <= len(events) - len(target):
        selected = events[index:index + len(target)]
        if tuple(event.kana for event in selected) == target:
            result.append(KanaCTCEvent(
                jaconv.hira2kata(mora),
                selected[0].start_sec,
                selected[-1].end_sec,
                math.exp(sum(math.log(max(event.confidence, 1e-300))
                             for event in selected) / len(selected)),
            ))
            index += len(target)
        else:
            index += 1
    return tuple(result)


def _prominent_peak_indices(
    values: Any, minimum_prominence: float, minimum_distance: int,
) -> list[int]:
    """Find separated one-dimensional peaks without an additional dependency."""
    candidates: list[int] = []
    floor = float(values.min())
    for index, value in enumerate(values):
        value = float(value)
        left_neighbor = float(values[index - 1]) if index else floor
        right_neighbor = float(values[index + 1]) if index + 1 < len(values) else floor
        if value < left_neighbor or value <= right_neighbor:
            continue
        left_base = floor if index == 0 else value
        cursor = index - 1
        while cursor >= 0 and float(values[cursor]) <= value:
            left_base = min(left_base, float(values[cursor]))
            cursor -= 1
        right_base = floor if index + 1 == len(values) else value
        cursor = index + 1
        while cursor < len(values) and float(values[cursor]) <= value:
            right_base = min(right_base, float(values[cursor]))
            cursor += 1
        if value - max(left_base, right_base) >= minimum_prominence:
            candidates.append(index)

    selected: list[int] = []
    for index in sorted(candidates, key=lambda item: float(values[item]), reverse=True):
        if all(abs(index - other) >= minimum_distance for other in selected):
            selected.append(index)
    return sorted(selected)


def collapse_kana_aliases(emissions: CTCEmissions) -> Any:
    """Sum mutually exclusive hiragana/katakana token probabilities.

    The vocabulary contains both scripts. They have the same pronunciation but
    need not have similar posteriors. Keep the original matrix unchanged for
    independent decoding; canonical target IDs receive their combined mass.
    """
    import numpy as np

    matrix = np.asarray(emissions.log_probs).copy()
    groups: dict[str, list[int]] = {}
    for token, token_id in emissions.vocab.items():
        kana = jaconv.hira2kata(token)
        if len(kana) == 1 and ("ァ" <= kana <= "ヺ" or kana == "ー"):
            groups.setdefault(kana, []).append(token_id)
    for kana, aliases in groups.items():
        if len(aliases) < 2:
            continue
        target = emissions.vocab.get(kana, aliases[0])
        combined = np.logaddexp.reduce(matrix[:, aliases], axis=1)
        matrix[:, aliases] = -np.inf
        matrix[:, target] = combined
    if hasattr(emissions.log_probs, "new_tensor"):
        return emissions.log_probs.new_tensor(matrix)
    return matrix


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

    from . import runproc

    logger.info("wav2vec2(%s)でCTC確率を計算中...", MODEL_NAME)
    cached = _MODEL_CACHE.get(device)
    if cached is None:
        model = Wav2Vec2ForCTC.from_pretrained(MODEL_NAME).eval().to(device)  # type: ignore[arg-type]
        processor = AutoProcessor.from_pretrained(MODEL_NAME)
        _MODEL_CACHE[device] = (model, processor)
    else:
        model, processor = cached

    audio, _ = librosa.load(str(vocals_path), sr=SAMPLING_RATE, mono=True)
    audio = np.pad(audio, pad_width=int(_PAD_SEC * SAMPLING_RATE))

    chunks: list[torch.Tensor] = []
    pos = 0
    n = len(audio)
    while pos < n:
        runproc.raise_if_cancelled()
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
    runproc.raise_if_cancelled()
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
    *,
    frame_offset: int = 0,
) -> list[AlignedMora]:
    """トークンスパンを(行,モーラ)ごとに集約してAlignedMora列にする。"""
    moras = [
        AlignedMora(line=li, mora=mi, kana=kana, start_sec=-1.0, end_sec=-1.0, score=0.0)
        for li, line in enumerate(line_moras)
        for mi, kana in enumerate(line)
    ]
    index = {(m.line, m.mora): m for m in moras}
    for span, owner in zip(spans, owners, strict=True):
        start = max(0.0, (span.start + frame_offset) * FRAME_SAMPLES / SAMPLING_RATE - _PAD_SEC)
        end = max(0.0, (span.end + frame_offset) * FRAME_SAMPLES / SAMPLING_RATE - _PAD_SEC)
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
    *,
    emissions: CTCEmissions | None = None,
    phonetic_aliases: bool = False,
    line_windows: list[tuple[float, float]] | None = None,
) -> tuple[list[AlignedMora], list[int]]:
    """行ごとの読み候補つきアライメント。

    line_variants[行] = 候補読みのモーラ列のリスト(先頭が既定)。
    候補が複数の行は、初回アライメントで得た行の時間範囲のlog_probsに
    候補ごとのforced_alignを掛け、尤度の高い読みを採用して最終アライメントする。
    line_windowsを指定した場合は各行をその音響区間内で対応づけ、絶対時刻を返す。
    区間は母音開始の観測ではなく、候補を支持した音声の範囲として扱う。

    戻り値: (全モーラの時刻列, 行ごとの採用候補index)
    """
    if line_windows is not None:
        if len(line_windows) != len(line_variants):
            raise ValueError("line_windows must contain one window per lyric line")
        line_windows = _validated_line_windows(line_windows)
    try:
        import torch
    except ImportError as e:
        raise RuntimeError(
            "torch/torchaudio/transformers がインストールされていません"
            "(uv sync --extra audio)"
        ) from e
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    emissions = emissions or compute_emissions(vocals_path, device)
    vocab, log_probs = emissions.vocab, emissions.log_probs
    if phonetic_aliases:
        log_probs = collapse_kana_aliases(emissions)

    if line_windows is not None:
        bounds = [(_frame_ceiling(start), _frame_ceiling(end))
                  for start, end in line_windows]
        if any(first >= last or first < 0 or last > len(log_probs) for first, last in bounds):
            raise ValueError("line_windows must contain available CTC frames")
        aligned: list[AlignedMora] = []
        choices: list[int] = []
        for line, (variants, window, frames) in enumerate(
            zip(line_variants, line_windows, bounds, strict=True)
        ):
            first, last = frames
            targets, _owners = build_targets([variants[0]], vocab)
            _require_ctc_capacity(last - first, targets, line=line)
            local, chosen = _align_variants(
                log_probs[first:last], vocab, [variants], frame_offset=first,
            )
            start, end = window
            aligned.extend(replace(mora, line=line,
                                   start_sec=max(start, mora.start_sec),
                                   end_sec=min(end, mora.end_sec)) for mora in local)
            choices.append(chosen[0])
        return aligned, choices
    return _align_variants(log_probs, vocab, line_variants)


def _pathological_reasons(
    moras: list[AlignedMora], window: tuple[float, float],
) -> list[str]:
    if not moras:
        return []
    reasons = []
    longest = max(moras, key=lambda mora: mora.end_sec - mora.start_sec)
    if (
        longest.end_sec - longest.start_sec > PATHOLOGICAL_MORA_SPAN_SEC
        and window[0] - longest.start_sec > PATHOLOGICAL_WINDOW_START_UNDERRUN_SEC
    ):
        reasons.append("mora-span-before-whisper-window")
    if max(mora.end_sec for mora in moras) - window[1] > PATHOLOGICAL_WINDOW_END_OVERRUN_SEC:
        reasons.append("after-whisper-window")
    return reasons


def retry_pathological_line_alignments(
    vocals_path: Path,
    line_variants: list[list[list[str]]],
    aligned: list[AlignedMora],
    line_windows: list[tuple[float, float]],
    device: str | None = None,
    *,
    emissions: CTCEmissions,
    phonetic_aliases: bool = False,
) -> tuple[list[AlignedMora], list[dict[str, object]]]:
    """Retry physically implausible whole-song assignments inside their ASR window.

    This is a safety net for a genuine overlap or other invalid line-window layout
    that forced the caller to use whole-song CTC.  A retry is published only when
    every pathology that triggered it is gone; otherwise the original alignment is
    retained.
    """
    if len(line_variants) != len(line_windows):
        raise ValueError("line_windows must contain one window per lyric line")
    grouped: list[list[AlignedMora]] = [[] for _ in line_variants]
    for mora in aligned:
        if not 0 <= mora.line < len(grouped):
            raise ValueError("aligned mora line is outside line_variants")
        grouped[mora.line].append(mora)

    replacements: dict[int, list[AlignedMora]] = {}
    diagnostics: list[dict[str, object]] = []
    for line, (variants, window, previous) in enumerate(
        zip(line_variants, line_windows, grouped, strict=True)
    ):
        reasons = _pathological_reasons(previous, window)
        if not reasons:
            continue
        before_max_span = max(
            (mora.end_sec - mora.start_sec for mora in previous), default=0.0
        )
        record: dict[str, object] = {
            "line": line,
            "window_start_sec": window[0],
            "window_end_sec": window[1],
            "reasons": reasons,
            "before_max_mora_span_sec": before_max_span,
            "before_line_end_sec": max(
                (mora.end_sec for mora in previous), default=window[0]
            ),
        }
        try:
            local, _choices = align_moras_with_variants(
                vocals_path,
                [variants],
                device=device,
                emissions=emissions,
                phonetic_aliases=phonetic_aliases,
                line_windows=[window],
            )
        except (RuntimeError, ValueError) as exc:
            record.update(status="failed", detail=str(exc))
            diagnostics.append(record)
            logger.warning("行%dの病的CTC整列を局所再試行できませんでした: %s", line, exc)
            continue
        local = [replace(mora, line=line) for mora in local]
        after_max_span = max(
            (mora.end_sec - mora.start_sec for mora in local), default=0.0
        )
        remaining = _pathological_reasons(local, window)
        record.update(
            after_max_mora_span_sec=after_max_span,
            after_line_end_sec=max(
                (mora.end_sec for mora in local), default=window[0]
            ),
        )
        if len(local) != len(previous) or remaining:
            record.update(
                status="unresolved",
                detail=(
                    "localized alignment did not preserve the mora count"
                    if len(local) != len(previous)
                    else f"remaining pathologies: {', '.join(remaining)}"
                ),
            )
            diagnostics.append(record)
            continue
        replacements[line] = local
        record["status"] = "replaced"
        diagnostics.append(record)
        logger.warning(
            "行%dの病的CTC整列をWhisper区間 %.3f–%.3f秒で再整列しました (%s)",
            line, window[0], window[1], ", ".join(reasons),
        )

    if not replacements:
        return aligned, diagnostics
    repaired = [
        mora
        for line in range(len(grouped))
        for mora in replacements.get(line, grouped[line])
    ]
    return repaired, diagnostics


def _align_variants(
    log_probs: Any, vocab: dict[str, int], line_variants: list[list[list[str]]], *,
    frame_offset: int = 0,
) -> tuple[list[AlignedMora], list[int]]:
    """Use the same pronunciation selection and CTC decoder at either scope."""

    chosen = [0] * len(line_variants)
    line_moras = [variants[0] for variants in line_variants]
    targets, owners = build_targets(line_moras, vocab)
    if not targets:
        raise ValueError("アライメント可能なカナがありません")
    _require_ctc_capacity(len(log_probs), targets)
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
            _require_ctc_capacity(len(log_probs), targets)
            spans = _forced_align(log_probs, targets)
            if len(spans) != len(targets):
                raise RuntimeError("再アライメントのトークン数が不一致")

    return _spans_to_moras(spans, owners, line_moras, frame_offset=frame_offset), chosen


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
