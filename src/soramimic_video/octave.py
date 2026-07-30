"""合成エンジン共通の自動音域調整(オクターブ移調 + 曲全体キー変更)。

歌声合成は各エンジンに「無理なく歌える音域」があり、そこを外れると
VOICEVOXはピッチが大きく崩れ、NEUTRINOは苦しそうな(力んだ)発声になる。
曲全体をオクターブ単位で移調して、その音域に最も多くの音符が収まるシフトを
選ぶことで、音痴・力み発声を避ける。エンジンごとに安全音域が違うため、
音域(key_min/key_max)を引数で受け取る形に一般化してある。

オクターブ単位に限っていたのは、伴奏(mix.pyが元MIDIをそのままの高さで
レンダリングする)と調を合わせるためだった。しかし音域の広い曲はどのオクターブ
でも収まらず、はみ出た音符でf0が崩れる。そこで「オクターブだけでは収まらない
場合に限り」歌と伴奏を同じだけ半音単位でずらす(カラオケのキー変更と同じ)。
このキー変更ぶんが ShiftPlan.key_shift で、mix が伴奏MIDIに同じ値を適用する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 実行時importは不要(型注釈のみ)
    from .project import Project

logger = logging.getLogger(__name__)

# VOICEVOX(歌の先生 6000)の安全音域。実測でこの外(MIDI 54〜78 = F#3〜F#5)は
# 要求keyに対しf0が大きく崩れる。voicevox.py が従来から使ってきた値。
VOICEVOX_SAFE_KEY_MIN = 54
VOICEVOX_SAFE_KEY_MAX = 78

# NEUTRINOの汎用歌唱音域(MIDI 50〜74 = D3〜D5、2オクターブ)。
# 同梱モデルの推奨音域(settings/model_info.json)を見ると、既定のMERROWがA3〜E5、
# 他モデルもおおむね女声がA3〜E5、男声がA2/C3〜C5に収まる。その中央付近を2オクターブで
# 取ったのがD3〜D5で、VOICEVOX(2オクターブ)と同じ幅にそろえてある。上端をD5に抑えて
# あるので、高すぎて力む曲は自動で1オクターブ下がる(ユーザー報告の「苦しそう」対策)。
# 単一の既定値。将来はモデル別の推奨音域を model 名で引く形へ拡張しやすいよう定数化した。
NEUTRINO_SAFE_KEY_MIN = 50
NEUTRINO_SAFE_KEY_MAX = 74

# 探索するオクターブシフト(半音)。従来から±2オクターブ。
OCTAVE_SHIFTS = (-24, -12, 0, 12, 24)

# 曲全体キー変更の上限(半音)。カラオケのキーコントロールと同じく±6程度に留める
# (これ以上ずらすと原曲の印象が変わりすぎる。半音6つで隣のオクターブに届く)。
MAX_KEY_SHIFT = 6


@dataclass(frozen=True)
class ShiftPlan:
    """自動音域調整の結果。

    vocal_shift: 歌に加算する移調量(半音)。= key_shift + 12 * オクターブ数。
    key_shift:   曲全体のキー変更(半音)。伴奏にも同じだけ適用する必要がある。
                 オクターブだけで安全音域に収まる曲では必ず 0。
    out_of_range: このシフトを適用してもなお安全音域を外れる音符数。
    """

    vocal_shift: int
    key_shift: int
    out_of_range: int


def auto_octave_shift(
    keys: list[int],
    transpose: int,
    key_min: int,
    key_max: int,
) -> int:
    """安全音域(key_min〜key_max)に収まる音符が最も多くなるオクターブシフト(半音)を返す。

    ユーザー指定のtransposeを適用した後のkeyに対して、-24〜+24半音の
    オクターブ単位で範囲外の音符数が最小になるシフトを選ぶ(同数なら0寄り)。
    キー変更は行わない(伴奏を移調できない経路と、従来の呼び出し元のため)。
    """
    return auto_shift_plan(
        keys, transpose, key_min, key_max, max_key_shift=0
    ).vocal_shift


def auto_shift_plan(
    keys: list[int],
    transpose: int,
    key_min: int,
    key_max: int,
    max_key_shift: int = MAX_KEY_SHIFT,
) -> ShiftPlan:
    """安全音域に収めるためのシフト(オクターブ + 必要ならキー変更)を決める。

    1. まず従来どおりオクターブ単位だけで探す。範囲外0件にできるならそれを採る
       (この場合 key_shift は 0 で、既存曲の挙動は完全に変わらない)。
    2. オクターブだけでは収まらない曲に限り、キー変更 k(±max_key_shift半音)と
       オクターブ o の組み合わせ(vocal_shift = k + 12o)を探索し、
       (範囲外音符数, |k|, |vocal_shift|) の辞書式最小を採る。キー変更は
       小さいほどよいので、同じ範囲外音符数ならキー変更なしが勝つ。
    3. どう動かしても収まらない極端な音域の曲は、範囲外が最小のものを採る
       (従来と同じく「最善を尽くす」)。
    """
    if not keys:
        return ShiftPlan(0, 0, 0)
    shifted = [k + transpose for k in keys]

    def out_count(shift: int) -> int:
        return sum(1 for k in shifted if not key_min <= k + shift <= key_max)

    octave = min(OCTAVE_SHIFTS, key=lambda x: (out_count(x), abs(x)))
    octave_out = out_count(octave)
    if octave_out == 0 or max_key_shift <= 0:
        return ShiftPlan(octave, 0, octave_out)

    best_out, _, _, vocal_shift, key_shift = min(
        (out_count(k + o), abs(k), abs(k + o), k + o, k)
        for k in range(-max_key_shift, max_key_shift + 1)
        for o in OCTAVE_SHIFTS
    )
    return ShiftPlan(vocal_shift, key_shift, best_out)


def resolve_auto_shift(
    project: Project,
    keys: list[int],
    transpose: int,
    key_min: int,
    key_max: int,
    engine: str,
) -> int:
    """自動音域調整のシフトを決め、キー変更を project に記録して歌の移調量を返す。

    伴奏を移調できる(元MIDIから伴奏をレンダリングする)プロジェクトでだけ
    キー変更を許可する。分離済み/レンダリング済みの伴奏wavを持つプロジェクト
    (analyze-audio、伴奏プリレンダ済みのanalyze-midi)は伴奏を後から移調できず、
    歌だけずらすと調が合わなくなるため、従来どおりオクターブ調整のみとする。
    """
    song = project.song
    can_transpose_backing = bool(song.midi_path) and not song.accompaniment_path
    plan = auto_shift_plan(
        keys,
        transpose,
        key_min,
        key_max,
        max_key_shift=MAX_KEY_SHIFT if can_transpose_backing else 0,
    )
    song.key_shift = plan.key_shift
    if plan.key_shift:
        logger.info(
            "%sの音域(MIDI %d〜%d)にオクターブ調整だけでは収まらないため、"
            "曲全体を%+d半音キー変更します(伴奏も同じだけ移調します)",
            engine, key_min, key_max, plan.key_shift,
        )
    if plan.vocal_shift - plan.key_shift:
        logger.info(
            "%sの音域(MIDI %d〜%d)に合わせて%+dオクターブ調整します",
            engine, key_min, key_max, (plan.vocal_shift - plan.key_shift) // 12,
        )
    if plan.out_of_range:
        logger.warning(
            "%sの音域(MIDI %d〜%d)に収まらない音符が%d個残ります(音域が広すぎる曲)",
            engine, key_min, key_max, plan.out_of_range,
        )
    return plan.vocal_shift
