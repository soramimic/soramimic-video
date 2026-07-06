"""メロディMIDI(非XF)を音源にアライメントし、ピッチ・タイミングを楽譜に寄せる。

issue #3。純音声推定(pyin中央値+CTCスパン)は精度が頭打ちのため、
普通のSMFがあれば問題を「採譜」から「楽譜と演奏のアライメント」に変える。

方針は「MIDIを基準にし、補正は大域的な線形写像(テンポスケール+オフセット)と
移調だけ」とする。当初はクロマDTWで写像を推定していたが、フレーム粒度(約0.2秒)が
モーラ間隔と同オーダーで局所的に歪み、モーラ→音符の対応を壊した
(XF正解での評価: ピッチ一致50%)。大域線形写像に置き換えると同じ曲で100%になる。

1. 候補チャンネルごとに、モーラ開始列と音符開始列の一致度を格子探索して
   線形写像(scale, offset)を推定する
2. CTCで得たモーラ開始時刻と写像済み音符開始時刻を、時刻+音高コストの
   単調DP(多対一: MIDIが同音連打を1音符にまとめた箇所に対応)でマッチング
3. ピッチはMIDIの音をそのまま採用(f0中央値との差から移調を自動補正)。
   対応する音符が無いモーラは f0 フォールバック。余った音符はメリスマとして
   kana="ー" の継続音符にする(XFフローと同じ表現)
4. 「被覆率+音高輪郭の一致度」が最良のチャンネルをメロディとして選ぶ
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from .audio_project import MoraNote
from .mora_align import AlignedMora

logger = logging.getLogger(__name__)

_DRUM_CHANNEL = 9
_MELISMA_GAP_SEC = 0.25  # 直前の音符とこれ以内に続く余り音符はメリスマとみなす
_SKIP_MORA_COST = 0.6
_SKIP_NOTE_COST = 0.4
_STAY_COST = 0.15  # 直前のモーラと音符を共有する遷移の固定ペナルティ
_PITCH_COST_W = 0.1  # DPの音高コスト重み(半音あたり。オクターブ無視・上限6半音)
_LEGATO_GAP_SEC = 0.15  # 音符間の隙間がこれ以下ならレガート接続(MIDIのゲート補正)


@dataclass
class MelodyNote:
    start_sec: float
    end_sec: float
    midi_note: int


def load_midi_notes(midi_path: Path) -> dict[int, list[MelodyNote]]:
    """チャンネルごとのノート列を秒単位で取り出す(テンポマップはmidoが解決)。"""
    import mido

    mid = mido.MidiFile(str(midi_path), clip=True)
    notes: dict[int, list[MelodyNote]] = {}
    pending: dict[tuple[int, int], float] = {}  # (channel, note) -> start_sec
    t = 0.0
    for msg in mid:  # MidiFileのイテレーションはtimeが秒になる
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            pending[(msg.channel, msg.note)] = t
        elif msg.type in ("note_off", "note_on"):  # note_on vel=0 はoff扱い
            start = pending.pop((msg.channel, msg.note), None)
            if start is not None and t > start:
                notes.setdefault(msg.channel, []).append(
                    MelodyNote(start_sec=start, end_sec=t, midi_note=msg.note)
                )
    for ch in notes:
        notes[ch].sort(key=lambda n: n.start_sec)
    return notes


def monophony_ratio(notes: list[MelodyNote]) -> float:
    """次のノート開始前に終わっているノートの割合(単旋律度)。"""
    if len(notes) < 2:
        return 1.0
    ok = sum(
        1 for a, b in zip(notes, notes[1:], strict=False) if a.end_sec <= b.start_sec + 1e-6
    )
    return ok / (len(notes) - 1)


def skyline(notes: list[MelodyNote]) -> list[MelodyNote]:
    """和音混じりのチャンネルから最高音の旋律線を取り出す(skyline法)。

    onlinesequencer等の「メロディ+伴奏が1チャンネル」のMIDI向け。
    同時に重なる音は高い方を残し、低い方は破棄(高い音が始まったら前の音を切り詰める)。
    """
    result: list[MelodyNote] = []
    for n in sorted(notes, key=lambda x: (x.start_sec, -x.midi_note)):
        if not result:
            result.append(MelodyNote(n.start_sec, n.end_sec, n.midi_note))
            continue
        cur = result[-1]
        if n.start_sec < cur.end_sec - 1e-6:
            if n.midi_note <= cur.midi_note:
                continue  # 旋律線の下の音
            cur.end_sec = n.start_sec  # 高い音に旋律が移った
            if cur.end_sec <= cur.start_sec:
                result.pop()
        result.append(MelodyNote(n.start_sec, n.end_sec, n.midi_note))
    return result


def fit_global_map(
    mora_onsets: list[float],
    note_onsets: list[float],
    scale_range: tuple[float, float] = (0.95, 1.05),
    offset_span_sec: float = 10.0,
) -> tuple[float, float]:
    """モーラ開始列と音符開始列が最も重なる線形写像 y=scale*x+offset を格子探索する。

    「MIDIへの補正は全体テンポとオフセットだけ」という前提の実装。
    スコアは写像した各音符から最近傍モーラまでの距離(0.5秒で飽和)の合計。
    粗い探索(0.1秒刻み)の後、最良点の周りを0.02秒刻みで詰める。
    """
    onsets = sorted(mora_onsets)

    def score(scale: float, offset: float) -> float:
        total = 0.0
        step = max(1, len(note_onsets) // 200)
        for t in note_onsets[::step]:
            y = scale * t + offset
            pos = bisect.bisect(onsets, y)
            cands = [abs(onsets[p] - y) for p in (pos - 1, pos) if 0 <= p < len(onsets)]
            total += min(min(cands), 0.5)
        return total

    best: tuple[float, float, float] | None = None
    scales = [s / 100 for s in range(round(scale_range[0] * 100), round(scale_range[1] * 100) + 1)]
    for scale in scales:
        base = median(mora_onsets) - scale * median(note_onsets)
        for k in range(-round(offset_span_sec * 10), round(offset_span_sec * 10) + 1):
            offset = base + k * 0.1
            s = score(scale, offset)
            if best is None or s < best[0]:
                best = (s, scale, offset)
    assert best is not None
    # 最良点の周りをオフセットのみ細かく詰める
    _, scale, offset = best
    for k in range(-8, 9):
        off = offset + k * 0.02
        s = score(scale, off)
        if s < best[0]:
            best = (s, scale, off)
    return best[1], best[2]


def match_moras_to_notes(
    mora_onsets: list[float],
    note_spans: list[tuple[float, float]],
    mora_pitches: list[int] | None = None,
    note_pitches: list[int] | None = None,
    transpose: int = 0,
    skip_mora_cost: float = _SKIP_MORA_COST,
    skip_note_cost: float = _SKIP_NOTE_COST,
    stay_cost: float = _STAY_COST,
    pitch_w: float = _PITCH_COST_W,
) -> list[tuple[int | None, int | None]]:
    """時刻(+音高)コストの単調DPマッチング。

    戻り値は (モーラindex, 音符index) のペア列。片方が None のものは
    対応なし(余りモーラ/余り音符)。

    - MIDIは同音連打を1つの長い音符にまとめることがあり音符数<モーラ数に
      なりうるため、「直前のモーラと同じ音符を共有する」遷移(stay)を許可する(多対一)
    - mora_pitches(f0由来)と note_pitches を渡すと、音高の不一致
      (オクターブ無視・上限6半音)もコストに加える。時刻が近い別の音符との
      取り違えを音高情報で防ぐ保険
    """
    m, n = len(mora_onsets), len(note_spans)
    inf = float("inf")
    use_pitch = mora_pitches is not None and note_pitches is not None and pitch_w > 0

    def pitch_cost(i: int, j: int) -> float:
        if not use_pitch:
            return 0.0
        assert mora_pitches is not None and note_pitches is not None
        d = mora_pitches[i] - (note_pitches[j] + transpose)
        octave_invariant = min(abs(d), abs(d - 12), abs(d + 12))
        return pitch_w * min(octave_invariant, 6)

    # dp[i][j]: モーラi個・音符j個を消費した最小コスト。bp[i][j]は遷移の種類
    dp = [[inf] * (n + 1) for _ in range(m + 1)]
    bp = [[0] * (n + 1) for _ in range(m + 1)]  # 1=match 2=skip_mora 3=skip_note 4=stay
    dp[0][0] = 0.0
    for i in range(m + 1):
        row, nxt = dp[i], dp[i + 1] if i < m else None
        onset = mora_onsets[i] if i < m else 0.0
        for j in range(n + 1):
            cur = row[j]
            if cur == inf:
                continue
            if nxt is not None and j < n:
                cost = cur + abs(onset - note_spans[j][0]) + pitch_cost(i, j)
                if cost < nxt[j + 1]:
                    nxt[j + 1] = cost
                    bp[i + 1][j + 1] = 1
            if nxt is not None:
                if j > 0:
                    start, end = note_spans[j - 1]
                    overflow = max(0.0, start - onset) + max(0.0, onset - end)
                    cost = cur + stay_cost + overflow + pitch_cost(i, j - 1)
                    if cost < nxt[j]:
                        nxt[j] = cost
                        bp[i + 1][j] = 4
                if cur + skip_mora_cost < nxt[j]:
                    nxt[j] = cur + skip_mora_cost
                    bp[i + 1][j] = 2
            if j < n and cur + skip_note_cost < row[j + 1]:
                row[j + 1] = cur + skip_note_cost
                bp[i][j + 1] = 3
    # バックトラック
    pairs: list[tuple[int | None, int | None]] = []
    i, j = m, n
    while i > 0 or j > 0:
        op = bp[i][j]
        if op == 1:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif op == 4:
            pairs.append((i - 1, j - 1))  # 音符j-1を共有
            i -= 1
        elif op == 2:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


def channel_match_score(
    pairs: list[tuple[int | None, int | None]],
    mora_count: int,
    fallback_midi: list[int],
    notes: list[MelodyNote],
) -> tuple[float, float, float]:
    """チャンネルの「メロディらしさ」をモーラとの照合結果で採点する。

    被覆率(マッチしたモーラの割合)が高く、f0由来の音高との差の
    ばらつき(MAD)が小さい(=移調を除いて輪郭が一致する)ほど高得点。
    ベースは拍が合っていても輪郭が合わないためMADで落ちる。
    戻り値: (score, coverage, mad)
    """
    matched = [(i, j) for i, j in pairs if i is not None and j is not None]
    coverage = len(matched) / max(1, mora_count)
    if not matched:
        return -1.0, 0.0, 0.0
    diffs = [fallback_midi[i] - notes[j].midi_note for i, j in matched]
    center = median(diffs)
    mad = median(abs(d - center) for d in diffs)
    return coverage - mad / 24.0, coverage, mad


def estimate_transpose(
    pairs: list[tuple[int | None, int | None]],
    fallback_midi: list[int],
    notes: list[MelodyNote],
) -> int:
    """f0由来の音高とMIDI音高の差の中央値(整数半音)。オクターブ違いのMIDI等を補正。"""
    diffs = [
        fallback_midi[i] - notes[j].midi_note
        for i, j in pairs
        if i is not None and j is not None
    ]
    return round(median(diffs)) if diffs else 0


def assemble_mora_notes(
    aligned: list[AlignedMora],
    notes: list[MelodyNote],
    pairs: list[tuple[int | None, int | None]],
    fallback_midi: list[int],
    transpose: int = 0,
) -> list[MoraNote]:
    """マッチング結果からMoraNote列を組み立てる。

    タイミングは楽譜(MIDI)基準: マッチしたモーラの開始・終端は写像済み音符の
    note-on/off をそのまま使う。CTC時刻を混ぜると音符単位でASRのジッタが乗り、
    「均等なテンポ感なのに微妙にずれる」歌唱になるため使わない。

    - マッチしたモーラ: ピッチ・開始・終端ともMIDI
    - 音符を共有するモーラ(同音連打がMIDIで1音符にまとまっている箇所):
      音符内の分割位置のみCTC時刻を使う(楽譜に分割の情報が無いため)
    - 余りモーラ: CTCタイミング+f0フォールバック
    - 余り音符: 直前の音符に間近で続くならメリスマ(kana="ー")、離れていれば間奏として破棄
    - 最後にMIDIのゲート由来の小さい隙間をレガート接続する
    """
    result: list[MoraNote] = []
    last_line = 0
    prev_j: int | None = None
    for i, j in pairs:
        if i is not None:
            m = aligned[i]
            last_line = m.line
            if j is not None:
                note = notes[j]
                if j == prev_j:  # 音符共有: 音符内の分割位置はCTC時刻
                    start = max(m.start_sec, note.start_sec)
                else:
                    start = note.start_sec
                end = max(note.end_sec, start + 0.05)
                pitch = note.midi_note + transpose
                prev_j = j
            else:
                start, end, pitch = m.start_sec, m.end_sec, fallback_midi[i]
            result.append(
                MoraNote(line=m.line, kana=m.kana, start_sec=start, end_sec=end,
                         midi_note=pitch)
            )
        else:
            assert j is not None
            note = notes[j]
            # メリスマは「直前の音符が終わったあと間近に続く」音符のみ。
            # 直前と重なって鳴る音(取り切れなかった和音)は挿入しない
            if (
                result
                and note.start_sec >= result[-1].end_sec - 0.05
                and note.start_sec - result[-1].end_sec <= _MELISMA_GAP_SEC
            ):
                result.append(
                    MoraNote(line=last_line, kana="ー", start_sec=note.start_sec,
                             end_sec=note.end_sec, midi_note=note.midi_note + transpose)
                )
            else:
                logger.debug("間奏の音符を破棄: %.2f-%.2fs", note.start_sec, note.end_sec)
            prev_j = j

    # MIDIのゲート(音符間の小さい隙間)をレガート接続。歌唱は基本レガートで、
    # 極短の休符はNEUTRINOでブツ切りに聞こえるため
    for a, b in zip(result, result[1:], strict=False):
        if 0 < b.start_sec - a.end_sec <= _LEGATO_GAP_SEC:
            a.end_sec = b.start_sec
    return result


def apply_melody_midi(
    audio_path: Path,
    midi_path: Path,
    channel: int | None,
    aligned: list[AlignedMora],
    fallback_midi: list[int],
) -> list[MoraNote]:
    """メロディMIDIでピッチ・タイミングを楽譜に寄せたMoraNote列を作る。

    audio_path は現在未使用(線形写像はモーラ開始列から推定する)だが、
    将来のwarp高度化(テンポ変化曲対応)のためにインターフェースは保持。
    """
    del audio_path
    notes_by_channel = load_midi_notes(midi_path)
    if channel is not None and channel not in notes_by_channel:
        raise ValueError(f"チャンネル{channel}にノートがありません")

    mora_onsets = [m.start_sec for m in aligned]

    def prepare(ch: int) -> list[MelodyNote]:
        """チャンネルを旋律線化し、線形写像で音源の時間軸に乗せる。"""
        notes = notes_by_channel[ch]
        if monophony_ratio(notes) < 0.95:
            notes = skyline(notes)
            logger.debug("ch%d: 和音混じりのためskylineで旋律線化 -> %d音", ch, len(notes))
        scale, offset = fit_global_map(mora_onsets, [n.start_sec for n in notes])
        logger.debug("ch%d: 線形写像 scale=%.3f offset=%+.2fs", ch, scale, offset)
        return [
            MelodyNote(
                start_sec=scale * n.start_sec + offset,
                end_sec=scale * n.end_sec + offset,
                midi_note=n.midi_note,
            )
            for n in notes
        ]

    if channel is not None:
        warped = prepare(channel)
    else:
        # モーラとの照合結果でメロディチャンネルを選ぶ。音数がモーラ数と
        # かけ離れたチャンネル(伴奏・装飾)は候補から外す
        best: tuple[float, int, list[MelodyNote]] | None = None
        for ch in sorted(notes_by_channel):
            if ch == _DRUM_CHANNEL:
                continue
            cand = prepare(ch)
            if not 0.25 * len(aligned) <= len(cand) <= 3 * len(aligned):
                continue
            cand_pairs = _match_pairs(mora_onsets, fallback_midi, cand)
            score, coverage, mad = channel_match_score(
                cand_pairs, len(aligned), fallback_midi, cand
            )
            logger.info(
                "ch%d: %d音 被覆率%.0f%% 音高輪郭MAD%.1f score=%.2f",
                ch, len(cand), coverage * 100, mad, score,
            )
            if best is None or score > best[0]:
                best = (score, ch, cand)
        if best is None:
            raise ValueError(
                "メロディ候補チャンネルがありません(--melody-channel で指定してください)"
            )
        _, channel, warped = best
    logger.info("メロディチャンネル: ch%d (%d音)", channel, len(warped))
    return finalize_melody(aligned, fallback_midi, warped)


def _match_pairs(
    mora_onsets: list[float],
    fallback_midi: list[int],
    notes: list[MelodyNote],
) -> list[tuple[int | None, int | None]]:
    """f0由来の移調推定込みで、モーラと音符を対応づける。"""
    transpose = round(median(fallback_midi) - median(n.midi_note for n in notes))
    return match_moras_to_notes(
        mora_onsets,
        [(n.start_sec, n.end_sec) for n in notes],
        mora_pitches=fallback_midi,
        note_pitches=[n.midi_note for n in notes],
        transpose=transpose,
    )


def finalize_melody(
    aligned: list[AlignedMora],
    fallback_midi: list[int],
    notes: list[MelodyNote],
) -> list[MoraNote]:
    """音源時間軸に乗った1本のメロディ音符列にモーラを対応づけて組み立てる。

    外部MIDI(warp後)と採譜pseudo-MIDI(同一時間軸)で共通の後段。
    """
    mora_onsets = [m.start_sec for m in aligned]
    pairs = _match_pairs(mora_onsets, fallback_midi, notes)
    matched = sum(1 for i, j in pairs if i is not None and j is not None)
    logger.info(
        "モーラ%d / 音符%d のうち %d 組が対応(モーラ被覆率 %.0f%%)",
        len(aligned), len(notes), matched, 100 * matched / max(1, len(aligned)),
    )
    transpose = estimate_transpose(pairs, fallback_midi, notes)
    if transpose:
        logger.warning("メロディと歌唱の音高差 %+d 半音を補正します", transpose)
    return assemble_mora_notes(aligned, notes, pairs, fallback_midi, transpose)


