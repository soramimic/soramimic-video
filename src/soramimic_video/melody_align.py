"""メロディMIDI(非XF)を音源にアライメントし、ピッチ・タイミングを楽譜に寄せる。

issue #3。純音声推定(pyin中央値+CTCスパン)は精度が頭打ちのため、
普通のSMFがあれば問題を「採譜」から「楽譜と演奏のアライメント」に変える:

1. MIDI全ノートのクロマ × 音源(元ミックス)の chroma_cqt を DTW で対応づけ、
   「MIDI時刻 → 実演奏時刻」の写像を得る
2. CTCで得たモーラ開始時刻(精密)と warp 済み音符開始時刻(粗い)を単調DPで対応づける
3. ピッチはMIDIの音をそのまま採用。対応する音符が無いモーラは f0 フォールバック。
   余った音符はメリスマとして kana="ー" の継続音符にする(XFフローと同じ表現)
4. f0中央値とMIDI音高の差からトランスポーズ(オクターブ違い等)を自動補正
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np

from .audio_project import MoraNote
from .mora_align import AlignedMora

logger = logging.getLogger(__name__)

DTW_HOP = 4096  # 22050Hzで約0.19秒。DTW行列が現実的なサイズに収まる粒度
DTW_SR = 22050
_DRUM_CHANNEL = 9
# CTCとMIDIの開始時刻差がこれ以内ならCTC(実際の歌い出し)を採用。
# warp済みMIDI時刻はDTWのフレーム粒度(約0.2s)の誤差を持つため、CTCを広めに信頼する
_ONSET_TRUST_SEC = 0.6
_MELISMA_GAP_SEC = 0.25  # 直前の音符とこれ以内に続く余り音符はメリスマとみなす
_SKIP_MORA_COST = 0.6
_SKIP_NOTE_COST = 0.4
_STAY_COST = 0.15  # 直前のモーラと音符を共有する遷移の固定ペナルティ
_LEGATO_GAP_SEC = 0.15  # 音符間の隙間がこれ以下ならレガート接続(MIDIのゲート補正)
# ピッチガード: 行の中でMIDIとf0の不一致(オクターブ無視で3半音以上)の割合が
# 高ければ、その行はMIDIの内容が歌と違う(ファンメイドMIDIの欠落・別旋律)と
# みなし行ごとf0にフォールバックする。別旋律でも部分的に音は一致するため、
# 連続不一致でなく行単位の不一致率で判定する。孤立した不一致(f0側の誤り)では
# 行の率が閾値に届かずMIDIが維持される
_PITCH_GUARD_SEMITONES = 3
_PITCH_GUARD_LINE_RATE = 0.35
_PITCH_GUARD_MIN_MATCHED = 4


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


def midi_chroma(notes: list[MelodyNote], n_frames: int, frame_sec: float) -> np.ndarray:
    """ノート列からピアノロール由来のクロマ(12, n_frames)を作る。"""
    chroma = np.zeros((12, n_frames))
    for n in notes:
        f0 = int(n.start_sec / frame_sec)
        f1 = max(f0 + 1, int(np.ceil(n.end_sec / frame_sec)))
        chroma[n.midi_note % 12, f0 : min(f1, n_frames)] += 1.0
    norm = np.linalg.norm(chroma, axis=0)
    norm[norm == 0] = 1.0
    return chroma / norm


def fill_silent_frames(chroma: np.ndarray) -> np.ndarray:
    """無音フレーム(ゼロベクトル)を一様ベクトルにする。

    ゼロベクトルはcosine距離がNaNになりDTWが失敗するため。
    一様ベクトルはどのフレームとも等距離なので経路をほぼ歪めない。
    """
    out = chroma.copy()
    silent = np.linalg.norm(out, axis=0) < 1e-9
    out[:, silent] = 1.0 / np.sqrt(out.shape[0])
    return out


def build_time_map(wp: np.ndarray, frame_sec: float) -> tuple[np.ndarray, np.ndarray]:
    """DTW経路(midiフレーム, audioフレーム)を単調な時刻対応表にする。

    同じmidiフレームに複数のaudioフレームが対応する場合は平均を取り、
    累積最大で単調化する。戻り値は (midi_times, audio_times)。
    """
    midi_frames = sorted(set(int(m) for m, _ in wp))
    audio_by_midi: dict[int, list[int]] = {m: [] for m in midi_frames}
    for m, a in wp:
        audio_by_midi[int(m)].append(int(a))
    midi_t = np.array(midi_frames, dtype=float) * frame_sec
    audio_t = np.array([np.mean(audio_by_midi[m]) for m in midi_frames]) * frame_sec
    audio_t = np.maximum.accumulate(audio_t)
    return midi_t, audio_t


def warp_sec(t: float, midi_times: np.ndarray, audio_times: np.ndarray) -> float:
    return float(np.interp(t, midi_times, audio_times))


def match_moras_to_notes(
    mora_onsets: list[float],
    note_spans: list[tuple[float, float]],
    skip_mora_cost: float = _SKIP_MORA_COST,
    skip_note_cost: float = _SKIP_NOTE_COST,
    stay_cost: float = _STAY_COST,
) -> list[tuple[int | None, int | None]]:
    """開始時刻差をコストにした単調DPマッチング。

    戻り値は (モーラindex, 音符index) のペア列。片方が None のものは
    対応なし(余りモーラ/余り音符)。

    MIDIは同音連打を1つの長い音符にまとめることがあり音符数<モーラ数に
    なりうるため、「直前のモーラと同じ音符を共有する」遷移(stay)を許可する
    (多対一)。stayのコストは固定ペナルティ+音符区間からのはみ出し距離。
    """
    m, n = len(mora_onsets), len(note_spans)
    inf = float("inf")
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
                cost = cur + abs(onset - note_spans[j][0])
                if cost < nxt[j + 1]:
                    nxt[j + 1] = cost
                    bp[i + 1][j + 1] = 1
            if nxt is not None:
                if j > 0:
                    start, end = note_spans[j - 1]
                    overflow = max(0.0, start - onset) + max(0.0, onset - end)
                    cost = cur + stay_cost + overflow
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

    - マッチしたモーラ: ピッチ=MIDI、開始=CTCとMIDIが近ければCTC、終端=MIDIのnote-off
    - 音符を共有するモーラ(同音連打がMIDIで1音符にまとまっている箇所): 開始=CTC
    - 余りモーラ: CTCタイミング+f0フォールバック
    - 余り音符: 直前の音符に間近で続くならメリスマ(kana="ー")、離れていれば間奏として破棄
    - 最後にMIDIのゲート由来の小さい隙間をレガート接続する
    """
    result: list[MoraNote] = []
    matched: list[tuple[int, int]] = []  # (resultのindex, f0フォールバック音高)
    last_line = 0
    prev_j: int | None = None
    for i, j in pairs:
        if i is not None:
            m = aligned[i]
            last_line = m.line
            if j is not None:
                note = notes[j]
                if j == prev_j:  # 音符共有: 開始は自分のCTC時刻
                    start = m.start_sec
                else:
                    close = abs(m.start_sec - note.start_sec) <= _ONSET_TRUST_SEC
                    start = m.start_sec if close else note.start_sec
                end = max(note.end_sec, start + 0.05)
                pitch = note.midi_note + transpose
                prev_j = j
                matched.append((len(result), fallback_midi[i]))
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

    guard_pitch_runs(result, matched)

    # MIDIのゲート(音符間の小さい隙間)をレガート接続。歌唱は基本レガートで、
    # 極短の休符はNEUTRINOでブツ切りに聞こえるため
    for a, b in zip(result, result[1:], strict=False):
        if 0 < b.start_sec - a.end_sec <= _LEGATO_GAP_SEC:
            a.end_sec = b.start_sec
    return result


def guard_pitch_runs(result: list[MoraNote], matched: list[tuple[int, int]]) -> None:
    """MIDIとf0の不一致率が高い行をf0にフォールバックする(インプレース)。

    matched: (resultのindex, そのモーラのf0フォールバック音高) の列。
    オクターブ違いは f0 のオクターブ誤りの可能性が高いので不一致とみなさない。
    """

    def disagree(idx: int, fb: int) -> bool:
        delta = result[idx].midi_note - fb
        octave_invariant = min(abs(delta), abs(delta - 12), abs(delta + 12))
        return octave_invariant >= _PITCH_GUARD_SEMITONES

    by_line: dict[int, list[tuple[int, int]]] = {}
    for idx, fb in matched:
        by_line.setdefault(result[idx].line, []).append((idx, fb))

    fallback_lines = []
    for line, entries in by_line.items():
        if len(entries) < _PITCH_GUARD_MIN_MATCHED:
            continue
        rate = sum(1 for idx, fb in entries if disagree(idx, fb)) / len(entries)
        if rate >= _PITCH_GUARD_LINE_RATE:
            for idx, fb in entries:
                result[idx].midi_note = fb
            fallback_lines.append(line)
    if fallback_lines:
        logger.warning(
            "MIDIと歌の旋律が合わない%d行をf0にフォールバックしました: 行%s",
            len(fallback_lines), fallback_lines,
        )


def apply_melody_midi(
    audio_path: Path,
    midi_path: Path,
    channel: int | None,
    aligned: list[AlignedMora],
    fallback_midi: list[int],
) -> list[MoraNote]:
    """メロディMIDIでピッチ・タイミングを楽譜に寄せたMoraNote列を作る。"""
    import librosa

    notes_by_channel = load_midi_notes(midi_path)
    if channel is not None and channel not in notes_by_channel:
        raise ValueError(f"チャンネル{channel}にノートがありません")

    # DTW: MIDI全ノートのクロマ × 元ミックスのクロマ
    logger.info("クロマDTWでMIDIを音源にアライメント中...")
    y, sr = librosa.load(str(audio_path), sr=DTW_SR, mono=True)
    chroma_audio = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=DTW_HOP)
    frame_sec = DTW_HOP / sr
    all_notes = [n for ch_notes in notes_by_channel.values() for n in ch_notes]
    n_midi_frames = int(np.ceil(max(n.end_sec for n in all_notes) / frame_sec)) + 1
    chroma_midi = midi_chroma(all_notes, n_midi_frames, frame_sec)
    _, wp = librosa.sequence.dtw(
        X=fill_silent_frames(chroma_midi),
        Y=fill_silent_frames(chroma_audio),
        metric="cosine",
    )
    midi_times, audio_times = build_time_map(wp[::-1], frame_sec)

    def prepare(ch: int) -> list[MelodyNote]:
        """チャンネルを旋律線化してwarpする。"""
        notes = notes_by_channel[ch]
        if monophony_ratio(notes) < 0.95:
            notes = skyline(notes)
            logger.debug("ch%d: 和音混じりのためskylineで旋律線化 -> %d音", ch, len(notes))
        return [
            MelodyNote(
                start_sec=warp_sec(n.start_sec, midi_times, audio_times),
                end_sec=warp_sec(n.end_sec, midi_times, audio_times),
                midi_note=n.midi_note,
            )
            for n in notes
        ]

    mora_onsets = [m.start_sec for m in aligned]

    if channel is not None:
        warped = prepare(channel)
        pairs = match_moras_to_notes(
            mora_onsets, [(n.start_sec, n.end_sec) for n in warped]
        )
    else:
        # モーラとの照合結果でメロディチャンネルを選ぶ。音数がモーラ数と
        # かけ離れたチャンネル(伴奏・装飾)は候補から外す
        best: tuple[float, int, list[MelodyNote], list] | None = None
        for ch in sorted(notes_by_channel):
            if ch == _DRUM_CHANNEL:
                continue
            cand = prepare(ch)
            if not 0.25 * len(aligned) <= len(cand) <= 3 * len(aligned):
                continue
            cand_pairs = match_moras_to_notes(
                mora_onsets, [(n.start_sec, n.end_sec) for n in cand]
            )
            score, coverage, mad = channel_match_score(
                cand_pairs, len(aligned), fallback_midi, cand
            )
            logger.info(
                "ch%d: %d音 被覆率%.0f%% 音高輪郭MAD%.1f score=%.2f",
                ch, len(cand), coverage * 100, mad, score,
            )
            if best is None or score > best[0]:
                best = (score, ch, cand, cand_pairs)
        if best is None:
            raise ValueError(
                "メロディ候補チャンネルがありません(--melody-channel で指定してください)"
            )
        _, channel, warped, pairs = best
    logger.info("メロディチャンネル: ch%d (%d音)", channel, len(warped))

    matched = sum(1 for i, j in pairs if i is not None and j is not None)
    logger.info(
        "モーラ%d / 音符%d のうち %d 組が対応(モーラ被覆率 %.0f%%)",
        len(aligned), len(warped), matched, 100 * matched / max(1, len(aligned)),
    )
    transpose = estimate_transpose(pairs, fallback_midi, warped)
    if transpose:
        logger.warning("MIDIと歌唱の音高差 %+d 半音を補正します", transpose)
    return assemble_mora_notes(aligned, warped, pairs, fallback_midi, transpose)
