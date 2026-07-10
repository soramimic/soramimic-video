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

import logging
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from . import runproc
from .kana import split_moras, vowel_of
from .project import Project

logger = logging.getLogger(__name__)

DEFAULT_ENGINE_URL = "http://127.0.0.1:50021"
FRAME_RATE = 93.75  # VOICEVOXの1フレーム = 1/93.75秒
# 歌唱用の「歌の先生」スタイル(sing型)。クエリ生成に使う。frame_synthesisは
# 選んだスタイル(frame_decode/sing)で行う。現状 sing型は 波音リツ ノーマル(6000)のみ。
SING_TEACHER_ID = 6000
_TIMEOUT = 120  # 1リクエストのタイムアウト秒(合成は数秒〜数十秒)


def _connect_error(engine_url: str, exc: Exception) -> RuntimeError:
    return RuntimeError(
        f"VOICEVOXエンジンに接続できません({engine_url})。"
        f"VOICEVOXアプリまたはengineを起動してください: {exc}"
    )


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
        if sf > cursor:  # 隙間を休符で埋める
            out_notes.append({"key": None, "frame_length": sf - cursor, "lyric": ""})

        kana = lyric_map.get(n.id) or ""
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


def run_voicevox(
    project: Project,
    project_dir: Path,
    engine_url: str = DEFAULT_ENGINE_URL,
    style_id: int = 3003,
    transpose: int = 0,
    progress_cb: Callable[[float], None] | None = None,
) -> Path:
    """VOICEVOXでvocal.wavを合成して返す。

    sing_frame_audio_query(歌の先生 6000。選んだstyle_id自体がsing型ならそれを先生に)
    → frame_synthesis(style_id) → vocal_path に書き出す。
    """
    from .synthesize import vocal_path

    base = engine_url.rstrip("/")
    score = build_score(project, transpose=transpose)

    # 先生(クエリ用スタイル)を決める。選んだスタイルがsing型なら自身、
    # そうでなければ歌の先生 6000。エンジンに繋がらなければ既定にフォールバック。
    teacher = SING_TEACHER_ID
    if style_id in _sing_style_ids(base):  # 繋がらなければRuntimeErrorで即失敗
        teacher = style_id
    runproc.raise_if_cancelled()

    try:
        r = requests.post(
            f"{base}/sing_frame_audio_query",
            params={"speaker": teacher},
            json=score,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise _connect_error(engine_url, exc) from exc
    if r.status_code != 200:
        raise RuntimeError(
            f"VOICEVOXのsing_frame_audio_queryが失敗しました({r.status_code}): {r.text[:500]}"
        )
    query = r.json()
    if progress_cb is not None:
        progress_cb(0.5)
    runproc.raise_if_cancelled()

    try:
        r2 = requests.post(
            f"{base}/frame_synthesis",
            params={"speaker": style_id},
            json=query,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise _connect_error(engine_url, exc) from exc
    if r2.status_code != 200:
        raise RuntimeError(
            f"VOICEVOXのframe_synthesisが失敗しました({r2.status_code}): {r2.text[:500]}"
        )

    wav = vocal_path(project_dir)
    _write_wav(wav, r2.content)
    if progress_cb is not None:
        progress_cb(1.0)
    with wave.open(str(wav)) as w:
        if w.getnframes() == 0:
            raise RuntimeError("VOICEVOXが空のWAVを返しました")
    logger.info("VOICEVOXで歌唱wavを合成しました: %s", wav)
    return wav
