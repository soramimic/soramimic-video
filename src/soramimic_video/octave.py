"""合成エンジン共通の自動オクターブ調整。

歌声合成は各エンジンに「無理なく歌える音域」があり、そこを外れると
VOICEVOXはピッチが大きく崩れ、NEUTRINOは苦しそうな(力んだ)発声になる。
曲全体をオクターブ単位で移調して、その音域に最も多くの音符が収まるシフトを
選ぶことで、音痴・力み発声を避ける。エンジンごとに安全音域が違うため、
音域(key_min/key_max)を引数で受け取る形に一般化してある。
"""

from __future__ import annotations

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


def auto_octave_shift(
    keys: list[int],
    transpose: int,
    key_min: int,
    key_max: int,
) -> int:
    """安全音域(key_min〜key_max)に収まる音符が最も多くなるオクターブシフト(半音)を返す。

    ユーザー指定のtransposeを適用した後のkeyに対して、-24〜+24半音の
    オクターブ単位で範囲外の音符数が最小になるシフトを選ぶ(同数なら0寄り)。
    """
    if not keys:
        return 0
    shifted = [k + transpose for k in keys]

    def out_count(shift: int) -> int:
        return sum(1 for k in shifted if not key_min <= k + shift <= key_max)

    return min((s * 12 for s in (-2, -1, 0, 1, 2)), key=lambda x: (out_count(x), abs(x)))
