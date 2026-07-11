"""VOICEVOX(歌声合成)のラッパー。

NEUTRINOより軽量な歌唱バックエンド。VOICEVOX ENGINE(HTTP)の歌唱API
(sing_frame_audio_query → frame_synthesis)を使う。エンジンは同梱せず、
VOICEVOXアプリまたはengineを起動して engine_url(既定 127.0.0.1:50021)で指す。

Score(楽譜)は VOICEVOX の frame ベース(93.75fps)。1音符=1モーラ厳守なので、
歌唱カナが複数モーラなら音符のフレームをモーラへ分配する。長音「ー」は直前の母音に
置換する(VOICEVOXは「ー」単体のlyricを受け付けない)。曲頭(t=0)から休符で埋めて
絶対時間を保つ(NEUTRINO経路と同じく、ミックスでの位置合わせを不要にする)。
"""

from __future__ import annotations

import io
import logging
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from . import runproc
from .kana import split_moras, vowel_of
from .project import Project

logger = logging.getLogger(__name__)

DEFAULT_ENGINE_URL = "http://127.0.0.1:50021"
FRAME_RATE = 93.75  # VOICEVOXの1フレーム = 1/93.75秒
DEFAULT_CHUNK_SEC = 60.0  # チャンク合成の1チャンク最大秒数(0以下で分割無効)
# 分割時、2番目以降のチャンク先頭に残す休符フレーム数(ボコーダの立ち上がり安定用)。
# エンジンは1フレーム休符を弾くため2以上必須。約0.3秒を目安にする。
LEAD_REST_FRAMES = max(2, round(0.3 * FRAME_RATE))
# 歌唱用の「歌の先生」スタイル(sing型)。クエリ生成に使う。frame_synthesisは
# 選んだスタイル(frame_decode/sing)で行う。現状 sing型は 波音リツ ノーマル(6000)のみ。
SING_TEACHER_ID = 6000
# 歌の先生(6000)が要求どおりのピッチで歌える音域(実測)。sing_frame_audio_queryの
# 返すf0を要求keyと比較したところ、54〜78(F#3〜F#5)の外では大きく崩れる
# (例: key=84で-9半音)。ハミングもf0は先生由来なので、この範囲が事実上の制約。
SAFE_KEY_MIN = 54
SAFE_KEY_MAX = 78
_TIMEOUT = 600  # 1リクエストのタイムアウト秒(フルコーラスのクエリ生成は数分かかりうる)
# エンジンは合成中にOOM等でクラッシュしうるが、launchd/dockerの再起動ポリシーで
# 自動復帰する運用。クライアント側は「復帰を待って同じチャンクを再試行」する。
# エンジンプロセスの起動・killはここでは絶対に行わない(HTTPで待つだけ)。
ENGINE_POLL_INTERVAL = 2.0  # エンジン復帰待ちの /version ポーリング間隔(秒)
ENGINE_RECOVERY_TIMEOUT = 60.0  # エンジン復帰待ちの上限(秒)
CHUNK_MAX_RETRIES = 2  # 接続断による1チャンクあたりの最大再試行回数


def _connect_error(engine_url: str, exc: Exception) -> RuntimeError:
    return RuntimeError(
        f"VOICEVOXエンジンに接続できません({engine_url})。"
        f"VOICEVOXアプリまたはengineを起動してください: {exc}"
    )


# 接続確立後にエンジンが応答を返さず切れたことを示すシグネチャ。歌唱合成中の
# OOMによる異常終了はこの形で観測される(RemoteDisconnected / Connection reset)。
_ABORTED_MARKERS = ("Connection aborted", "RemoteDisconnected", "ConnectionResetError")


def _request_error(engine_url: str, exc: Exception) -> RuntimeError:
    """合成リクエスト中の例外を、接続断(異常終了)と接続不可で切り分ける。

    接続確立後にエンジンが落ちた場合(RemoteDisconnected等)はメモリ不足を疑う
    案内を出す。接続自体ができない場合は通常の接続エラー案内を返す。
    """
    msg = str(exc)
    if any(marker in msg for marker in _ABORTED_MARKERS):
        return RuntimeError(
            "VOICEVOXエンジンが処理中に異常終了しました。メモリ不足の可能性があります。"
            f"エンジンを再起動して再実行してください: {exc}"
        )
    return _connect_error(engine_url, exc)


def _wait_for_engine(base: str, timeout: float = ENGINE_RECOVERY_TIMEOUT) -> bool:
    """クラッシュしたエンジンの復帰を GET /version のポーリングで待つ。

    timeout 秒以内に /version が 200 を返せば True、上限に達したら False を返す。
    ENGINE_POLL_INTERVAL 間隔でポーリングし、待機中も runproc.raise_if_cancelled()
    を呼んでキャンセルを効かせる。/version が返ればモデル未ロードでも合成は通るので、
    復帰判定はこれで十分(エンジンプロセスの起動・killは一切行わない)。
    """
    deadline = time.monotonic() + timeout
    while True:
        runproc.raise_if_cancelled()
        try:
            r = requests.get(f"{base}/version", timeout=ENGINE_POLL_INTERVAL)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass  # まだ復帰していない(接続拒否など)。上限まで待つ。
        if time.monotonic() >= deadline:
            return False
        time.sleep(ENGINE_POLL_INTERVAL)


def split_voicevox_moras(kana: str) -> list[str]:
    """VOICEVOXのlyric用にモーラ分割する(1要素=1モーラ)。

    拗音(小書きカナ)は直前にまとめ、長音「ー」は直前モーラの母音に置換して
    独立モーラにする(VOICEVOXは「ー」を含むlyricを弾くため)。
    ッ・ンは独立モーラ。
    """
    out: list[str] = []
    for mora in split_moras(kana):
        base = mora.rstrip("ー")
        n_long = len(mora) - len(base)
        if base:
            out.append(base)
        elif n_long:
            # 先頭が「ー」のみ(前音の母音を伸ばす継続モーラ)。母音1つに落とす。
            out.append((vowel_of(out[-1]) if out else None) or "ア")
            n_long -= 1
        if n_long:
            v = vowel_of(base) if base else (out[-1] if out else "ア")
            out.extend([v or "ア"] * n_long)
    return out


def auto_octave_shift(keys: list[int], transpose: int = 0) -> int:
    """安全音域に収まる音符が最も多くなるオクターブシフト(半音)を返す。

    ユーザー指定のtransposeを適用した後のkeyに対して、-24〜+24半音の
    オクターブ単位で範囲外の音符数が最小になるシフトを選ぶ(同数なら0寄り)。
    """
    if not keys:
        return 0
    shifted = [k + transpose for k in keys]

    def out_count(shift: int) -> int:
        return sum(
            1 for k in shifted if not SAFE_KEY_MIN <= k + shift <= SAFE_KEY_MAX
        )

    return min((s * 12 for s in (-2, -1, 0, 1, 2)), key=lambda x: (out_count(x), abs(x)))


def build_score(project: Project, transpose: int = 0) -> dict[str, Any]:
    """projectからVOICEVOXのScore(dict)を作る。

    - 曲頭(t=0)から休符で埋める(絶対時間を保つ)。
    - 音符の重なりは後勝ちでクリップ、隙間は休符で埋める。
    - 1音符=1モーラ。複数モーラのカナは音符フレームを分配する。
    - transposeは非休符のkeyに半音単位で加算。
    """
    from .synthesize import build_lyric_map

    lyric_map = build_lyric_map(project)
    notes = sorted(project.notes, key=lambda n: n.start_tick)

    out_notes: list[dict[str, Any]] = []
    cursor = 0  # 出力済みの絶対フレーム位置
    prev_vowel = "ア"

    def frame(sec: float) -> int:
        return round(sec * FRAME_RATE)

    for n in notes:
        sf = frame(n.start_sec)
        ef = frame(n.end_sec)
        if sf < cursor:  # 重なり: 前音に食い込む分を切り詰め
            sf = cursor
        if ef <= sf:  # 長さが無い(丸めで消えた)音符は捨てる
            continue
        if sf - cursor == 1:
            # 1フレームだけの休符はエンジンが500を返す(実測: 2フレーム以上は可)。
            # 丸めで生じた微小ギャップなので、直前の要素を1フレーム伸ばして埋める
            if out_notes:
                out_notes[-1]["frame_length"] += 1
            else:
                sf = cursor  # 曲頭なら音符を1フレーム前倒しする
        elif sf > cursor:  # 隙間を休符で埋める
            out_notes.append({"key": None, "frame_length": sf - cursor, "lyric": ""})

        kana = lyric_map.get(n.id) or ""
        if kana.startswith("ー"):
            # 伸ばしノート(「ー」等): split_voicevox_morasは文字列内でしか前音を
            # 知れず「ア」に落ちてしまうため、直前ノートの母音で置き換える
            kana = prev_vowel + kana[1:]
        morae = split_voicevox_moras(kana)
        if not morae:  # カナが無い継続モーラ等: 直前の母音を引き継ぐ
            morae = [prev_vowel]
        total = ef - sf
        m = len(morae)
        for i, mora in enumerate(morae):
            b0 = sf + round(total * i / m)
            b1 = sf + round(total * (i + 1) / m)
            length = b1 - b0
            if length <= 0:  # モーラが多すぎてフレームが足りない場合は最低1
                length = 1
            out_notes.append(
                {"key": n.midi_note + transpose, "frame_length": length, "lyric": mora}
            )
        prev_vowel = vowel_of(morae[-1]) or prev_vowel
        cursor = ef

    if not out_notes:
        raise ValueError("音符がありません")
    return {"notes": out_notes}


@dataclass
class ScoreChunk:
    """分割後のスコアチャンク。start_frameは曲頭からの絶対開始フレーム。"""

    start_frame: int
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def frame_length(self) -> int:
        return sum(n["frame_length"] for n in self.notes)

    def to_score(self) -> dict[str, Any]:
        return {"notes": self.notes}


def split_score(
    score: dict[str, Any],
    max_sec: float = DEFAULT_CHUNK_SEC,
    min_rest_sec: float = 0.5,
) -> list[ScoreChunk]:
    """build_scoreのスコアを休符境界でチャンクに分割する純関数。

    - 分割位置は休符(key=None)の途中または境界のみ。min_rest_sec以上の休符だけを
      分割候補とし、チャンクがmax_secを超えないよう詰める。分割候補が無ければ超過を許す
      (音符の途中では絶対に切らない)。
    - 各チャンクは絶対開始フレーム(start_frame)を持ち、全チャンクのframe_length合計と
      並びは元スコアと完全一致する(分割・結合で絶対時間がずれない)。
    - 2番目以降のチャンクは先頭に休符(>=LEAD_REST_FRAMES)を持つ。エンジンは1フレーム
      休符を弾くため、境界を割るときも1フレーム休符は作らない。
    """
    notes = score["notes"]
    max_frames = max_sec * FRAME_RATE
    min_rest_frames = min_rest_sec * FRAME_RATE
    lead = LEAD_REST_FRAMES

    # まず絶対フレームでの分割位置(cut frames)を決める。cutは必ず休符の内部か境界。
    cuts: list[int] = []
    chunk_start = 0
    best_cut: int | None = None  # 現チャンクを max 以内に保てる直近の候補cut
    abs_f = 0
    for seg in notes:
        a = abs_f
        length = seg["frame_length"]
        end = a + length
        is_candidate = seg["key"] is None and length >= min_rest_frames
        if is_candidate:
            # このチャンクに残す休符を lead 以上にする(次チャンク先頭の休符)。
            seg_lead = min(lead, length - 2) if length >= 4 else max(1, length - 1)
            latest_s = a + length - seg_lead  # このチャンクに一番詰めたときのcut
            budget_s = int(chunk_start + max_frames)
            if latest_s <= budget_s:
                # maxに収まる: よりmaxに近い(=より遅い)候補として記憶
                s = latest_s
                if s - a == 1:  # 1フレーム休符を前チャンク末尾に作らない
                    s = a
                best_cut = s
            else:
                # この候補で初めてmaxに達する/超える。maxに最も近い位置で確定させる。
                s = budget_s
                if s < a:  # 既にmax超過(直前が長い音符列): 休符頭で切って肥大を止める
                    s = a
                if s - a == 1:
                    s = a
                cuts.append(s)
                chunk_start = s
                best_cut = None
                abs_f = end
                continue
        if end - chunk_start > max_frames and best_cut is not None:
            # 現セグメントでmax超過。max以内に保てる直近候補で確定する。
            cuts.append(best_cut)
            chunk_start = best_cut
            best_cut = None
        abs_f = end

    return _slice_by_cuts(notes, cuts)


def _slice_by_cuts(
    notes: list[dict[str, Any]], cuts: list[int]
) -> list[ScoreChunk]:
    """絶対フレームのcut位置でnotesを分割する。cutは休符の内部/境界にある前提。"""
    cuts = sorted(cuts)
    chunks: list[ScoreChunk] = [ScoreChunk(start_frame=0)]
    ci = 0
    abs_f = 0
    for seg in notes:
        a = abs_f
        length = seg["frame_length"]
        end = a + length
        # 境界(cut == セグメント先頭)の処理: ここから新チャンク。
        while ci < len(cuts) and cuts[ci] <= a:
            if cuts[ci] == a and a != chunks[-1].start_frame:
                chunks.append(ScoreChunk(start_frame=a))
            ci += 1
        if ci < len(cuts) and a < cuts[ci] < end:
            # 休符の途中で分割: 前半は現チャンク、後半は次チャンク先頭の休符。
            s = cuts[ci]
            chunks[-1].notes.append(
                {"key": seg["key"], "frame_length": s - a, "lyric": seg["lyric"]}
            )
            chunks.append(ScoreChunk(start_frame=s))
            chunks[-1].notes.append(
                {"key": seg["key"], "frame_length": end - s, "lyric": seg["lyric"]}
            )
            ci += 1
        else:
            chunks[-1].notes.append(dict(seg))
        abs_f = end
    return chunks


def list_singers(
    engine_url: str = DEFAULT_ENGINE_URL, timeout: float = 5.0
) -> list[dict[str, Any]]:
    """歌唱可能なスタイル一覧(sing / frame_decode)を返す。

    各要素: {name(キャラ名), style_name(スタイル名), style_id, type}。
    sing(実歌唱声)を先頭に、続いてハミング(frame_decode)を並べる。
    """
    try:
        r = requests.get(f"{engine_url.rstrip('/')}/singers", timeout=timeout)
        r.raise_for_status()
        singers = r.json()
    except requests.RequestException as exc:
        raise _connect_error(engine_url, exc) from exc
    sing: list[dict[str, Any]] = []
    humming: list[dict[str, Any]] = []
    for sp in singers:
        for st in sp.get("styles", []):
            t = st.get("type")
            if t not in ("sing", "frame_decode"):
                continue
            item = {
                "name": sp.get("name", ""),
                "style_name": st.get("name", ""),
                "style_id": st.get("id"),
                "type": t,
            }
            (sing if t == "sing" else humming).append(item)
    return sing + humming


def _sing_style_ids(engine_url: str) -> set[int]:
    return {s["style_id"] for s in list_singers(engine_url) if s["type"] == "sing"}


def _write_wav(path: Path, wav_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes)


def _read_wav(wav_bytes: bytes) -> tuple[int, int, int, int, bytes]:
    """WAVバイト列を (sample_rate, sampwidth, nchannels, nframes, pcm) に分解する。"""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return (
            w.getframerate(),
            w.getsampwidth(),
            w.getnchannels(),
            w.getnframes(),
            w.readframes(w.getnframes()),
        )


def _samples_per_frame(sample_rate: int) -> int:
    """1スコアフレームあたりのサンプル数。整数にならなければ結合不能でエラー。"""
    spf = sample_rate / FRAME_RATE
    if spf != int(spf):
        raise RuntimeError(
            f"サンプルレート{sample_rate}はフレームレート{FRAME_RATE}で割り切れず、"
            "チャンク結合できません"
        )
    return int(spf)


def _concat_chunks(
    wav_parts: list[bytes | None], chunks: list[ScoreChunk], total_frames: int
) -> bytes:
    """各チャンクのWAVを絶対フレーム位置で連結し、1本のWAVバイト列にする。

    frame→サンプル変換は samples_per_frame = sr/FRAME_RATE。各チャンクの実サンプル数が
    期待(frame_length×spf)と丸め起因でずれる場合のみ黙ってpad/trimする。
    wav_partsのNoneは合成をスキップした純休符チャンク。出力バッファはゼロ初期化なので
    書き込みを省略するだけで無音になる。WAVフォーマットの基準は最初の非Noneから取る。
    """
    first = next((p for p in wav_parts if p is not None), None)
    if first is None:  # build_scoreが音符ゼロを弾くため通常は起こらない(防御)
        raise RuntimeError("全チャンクが休符のみで、合成するチャンクがありません")
    sr, sampwidth, nchannels, _, _ = _read_wav(first)
    spf = _samples_per_frame(sr)
    tolerance = spf  # 許容ドリフト(約1フレーム分)。これを超えるずれはエラー。
    bytes_per_sample = sampwidth * nchannels
    out = bytearray(total_frames * spf * bytes_per_sample)
    for part, chunk in zip(wav_parts, chunks, strict=True):
        if part is None:  # 純休符チャンク: 無音のまま
            continue
        p_sr, p_sw, p_ch, p_nframes, pcm = _read_wav(part)
        if (p_sr, p_sw, p_ch) != (sr, sampwidth, nchannels):
            raise RuntimeError("チャンク間でWAVフォーマットが一致しません")
        expected = chunk.frame_length * spf
        drift = p_nframes - expected
        if abs(drift) > tolerance:
            raise RuntimeError(
                f"チャンクのサンプル数が期待{expected}に対し{p_nframes}で"
                f"許容誤差({tolerance})を超えています"
            )
        expected_bytes = expected * bytes_per_sample
        if len(pcm) > expected_bytes:  # 丸め起因の余剰をtrim
            pcm = pcm[:expected_bytes]
        elif len(pcm) < expected_bytes:  # 丸め起因の不足を無音でpad
            pcm = pcm + b"\x00" * (expected_bytes - len(pcm))
        offset = chunk.start_frame * spf * bytes_per_sample
        out[offset : offset + expected_bytes] = pcm

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(sr)
        w.writeframes(bytes(out))
    return buf.getvalue()


def run_voicevox(
    project: Project,
    project_dir: Path,
    engine_url: str = DEFAULT_ENGINE_URL,
    style_id: int = 3003,
    transpose: int = 0,
    auto_octave: bool = True,
    progress_cb: Callable[[float], None] | None = None,
    chunk_sec: float = DEFAULT_CHUNK_SEC,
) -> Path:
    """VOICEVOXでvocal.wavを合成して返す。

    sing_frame_audio_query(歌の先生 6000。選んだstyle_id自体がsing型ならそれを先生に)
    → frame_synthesis(style_id) → vocal_path に書き出す。
    auto_octave(既定ON)は、安全音域(SAFE_KEY_MIN..MAX)に収まる音符が最大に
    なるようオクターブ単位で自動移調する(範囲外はピッチが大きく崩れるため)。
    chunk_sec>0 のときはスコアを休符境界で分割し、チャンクごとに順次合成して結合する
    (エンジンのピークメモリを抑えてOOMを避ける)。0以下で分割無効(従来どおり1リクエスト)。
    合成中にエンジンが接続断(クラッシュ)した場合は、GET /version の復帰を待って同じ
    チャンクを再試行する(最大 CHUNK_MAX_RETRIES 回。復帰待ちの上限は ENGINE_RECOVERY_TIMEOUT)。
    """
    from .synthesize import vocal_path

    base = engine_url.rstrip("/")
    if auto_octave:
        shift = auto_octave_shift([n.midi_note for n in project.notes], transpose)
        if shift:
            logger.info(
                "VOICEVOXの音域(MIDI %d〜%d)に合わせて%+dオクターブ調整します",
                SAFE_KEY_MIN, SAFE_KEY_MAX, shift // 12,
            )
            transpose += shift
    score = build_score(project, transpose=transpose)

    # 先生(クエリ用スタイル)を決める。選んだスタイルがsing型なら自身、
    # そうでなければ歌の先生 6000。エンジンに繋がらなければ既定にフォールバック。
    teacher = SING_TEACHER_ID
    if style_id in _sing_style_ids(base):  # 繋がらなければRuntimeErrorで即失敗
        teacher = style_id
    runproc.raise_if_cancelled()

    if chunk_sec > 0:
        chunks = split_score(score, max_sec=chunk_sec)
    else:
        chunks = [ScoreChunk(start_frame=0, notes=score["notes"])]
    logger.info("VOICEVOX合成: %dチャンクに分割しました", len(chunks))

    wav_parts: list[bytes | None] = []
    for i, chunk in enumerate(chunks):
        runproc.raise_if_cancelled()
        if any(n["key"] is not None for n in chunk.notes):
            wav_parts.append(
                _synthesize_chunk_resilient(
                    base, engine_url, teacher, style_id, chunk.to_score()
                )
            )
        else:
            # 純休符チャンク(長いイントロ・間奏)は合成せず無音として扱う。
            logger.info(
                "チャンク%d/%dは休符のみのため合成をスキップします", i + 1, len(chunks)
            )
            wav_parts.append(None)
        if progress_cb is not None:
            progress_cb((i + 1) / len(chunks))

    if len(chunks) == 1 and wav_parts[0] is not None:
        # 1チャンクなら結合不要。エンジン出力をそのまま書く(従来動作を維持)。
        content = wav_parts[0]
    else:
        total_frames = sum(c.frame_length for c in chunks)
        content = _concat_chunks(wav_parts, chunks, total_frames)

    wav = vocal_path(project_dir)
    _write_wav(wav, content)
    with wave.open(str(wav)) as w:
        if w.getnframes() == 0:
            raise RuntimeError("VOICEVOXが空のWAVを返しました")
    logger.info("VOICEVOXで歌唱wavを合成しました: %s", wav)
    return wav


def _synthesize_chunk_resilient(
    base: str,
    engine_url: str,
    teacher: int,
    style_id: int,
    score: dict[str, Any],
) -> bytes:
    """_synthesize_chunk を接続断に強くしたラッパー。

    合成中にエンジンがクラッシュすると requests.ConnectionError になる(合成中の異常終了、
    死んでいる間のリクエストの両方)。その場合は GET /version の復帰を待って同じチャンクを
    最大 CHUNK_MAX_RETRIES 回まで再試行する。復帰待ちがタイムアウトした、または再試行を
    使い切った場合は _request_error 相当の RuntimeError を送出する。接続系以外のエラー
    (HTTPステータス異常・タイムアウト)はリトライせず即失敗する(挙動を変えない)。
    """
    for attempt in range(CHUNK_MAX_RETRIES + 1):
        try:
            return _synthesize_chunk(base, engine_url, teacher, style_id, score)
        except requests.ConnectionError as exc:
            if attempt >= CHUNK_MAX_RETRIES:
                raise _request_error(engine_url, exc) from exc
            logger.warning(
                "VOICEVOXエンジンとの接続が切れました。復帰を待って再試行します"
                "(%d/%d): %s",
                attempt + 1, CHUNK_MAX_RETRIES, exc,
            )
            if not _wait_for_engine(base):
                # 上限まで待っても /version が戻らない。異常終了・再起動案内で失敗させる。
                raise _request_error(engine_url, exc) from exc
    # ループは必ず return か raise で抜けるが、型チェッカーのため防御的に置く。
    raise RuntimeError("VOICEVOXチャンク合成が予期せず終了しました")


def _synthesize_chunk(
    base: str,
    engine_url: str,
    teacher: int,
    style_id: int,
    score: dict[str, Any],
) -> bytes:
    """1チャンクを sing_frame_audio_query → frame_synthesis して WAV バイトを返す。"""
    try:
        r = requests.post(
            f"{base}/sing_frame_audio_query",
            params={"speaker": teacher},
            json=score,
            timeout=_TIMEOUT,
        )
    except requests.ConnectionError:
        # 接続断(合成中のクラッシュ or 死んでいる間のリクエスト)は呼び出し側で
        # 復帰待ち・再試行するため、そのまま送出する。
        raise
    except requests.RequestException as exc:
        raise _request_error(engine_url, exc) from exc
    if r.status_code != 200:
        raise RuntimeError(
            f"VOICEVOXのsing_frame_audio_queryが失敗しました({r.status_code}): {r.text[:500]}"
        )
    query = r.json()

    try:
        r2 = requests.post(
            f"{base}/frame_synthesis",
            params={"speaker": style_id},
            json=query,
            timeout=_TIMEOUT,
        )
    except requests.ConnectionError:
        # 接続断(合成中のクラッシュ or 死んでいる間のリクエスト)は呼び出し側で
        # 復帰待ち・再試行するため、そのまま送出する。
        raise
    except requests.RequestException as exc:
        raise _request_error(engine_url, exc) from exc
    if r2.status_code != 200:
        raise RuntimeError(
            f"VOICEVOXのframe_synthesisが失敗しました({r2.status_code}): {r2.text[:500]}"
        )
    return r2.content
