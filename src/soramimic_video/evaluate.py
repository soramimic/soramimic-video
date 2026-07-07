"""音源解析(analyze-audio)の出力をXF正解と突き合わせて評価する。

XF MIDIがある曲なら「モーラ↔音符↔音高」の正解が作れる(analyzeステージの出力)。
音源+非XF MIDI経路の出力(推定)を、カナ列のDP対応付け(difflib)で正解と対応づけ、
ピッチ正解率とタイミング残差を数値で出す。試聴に頼らず改善の効果を判定するための
ハーネス(issue #3)。

タイミングについて: 正解(XF)の時間軸と音源の時間軸はイントロの長さ等で
ずれていることがあるため、対応ペアから中央値オフセットを推定して差し引いた
残差で評価する。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from statistics import median

from .kana import normalize_long_vowels
from .project import Project


@dataclass
class EvalResult:
    n_truth: int
    n_est: int
    n_matched: int
    coverage_truth: float  # 正解音符のうち対応がついた割合
    transpose: int  # 推定-正解の音高オフセット中央値(半音)
    pitch_acc: float  # 移調を除いた完全一致率
    pitch_acc_within1: float  # 移調を除き±1半音以内
    time_offset_sec: float  # 時間軸オフセット(推定-正解)の中央値
    onset_mae_sec: float  # オフセット補正後の開始時刻の平均絶対誤差
    onset_p90_sec: float  # 同90パーセンタイル

    def summary(self) -> str:
        return "\n".join([
            f"対応: {self.n_matched}/{self.n_truth}正解音符 "
            f"(被覆率{self.coverage_truth:.0%}, 推定側{self.n_est}音符)",
            f"ピッチ: 移調{self.transpose:+d}半音を除いて "
            f"完全一致{self.pitch_acc:.0%} / ±1半音{self.pitch_acc_within1:.0%}",
            f"タイミング: 時間軸オフセット{self.time_offset_sec:+.2f}秒 / "
            f"補正後MAE {self.onset_mae_sec*1000:.0f}ms / p90 {self.onset_p90_sec*1000:.0f}ms",
        ])


def _chars_with_owner(kanas: list[str]) -> tuple[list[str], list[int]]:
    """カナ列を正規化した文字列に展開し、各文字の元要素indexを返す。"""
    owners = [i for i, k in enumerate(kanas) for _ in k]
    chars = list(normalize_long_vowels("".join(kanas)))
    return chars, owners


def match_by_kana(
    truth_kanas: list[str], est_kanas: list[str]
) -> list[tuple[int, int]]:
    """カナ列同士をDP対応付けし、一致した (正解idx, 推定idx) ペアを返す。

    読みエンジンによって長音の表記(ヨウ/ヨー)やモーラの切り方
    (ヨ+ウの2音符 vs ヨー+継続ーの2音符)が違っても対応が切れないよう、
    要素単位でなく正規化した文字単位でマッチングし、要素indexに引き戻す。
    正解側は一意、推定側は再利用を許す(長音がまとまった音符に複数の正解音符が
    対応するケース)。
    """
    t_chars, t_owners = _chars_with_owner(truth_kanas)
    e_chars, e_owners = _chars_with_owner(est_kanas)
    sm = difflib.SequenceMatcher(a=t_chars, b=e_chars, autojunk=False)
    pairs: list[tuple[int, int]] = []
    seen_truth: set[int] = set()
    for block in sm.get_matching_blocks():
        for k in range(block.size):
            ti, ei = t_owners[block.a + k], e_owners[block.b + k]
            if ti not in seen_truth:
                seen_truth.add(ti)
                pairs.append((ti, ei))
    return pairs


def evaluate(truth: Project, est: Project) -> EvalResult:
    truth_kanas = [n.kana for n in truth.notes]
    est_kanas = [n.kana for n in est.notes]
    pairs = match_by_kana(truth_kanas, est_kanas)
    if not pairs:
        raise ValueError("正解と推定でカナの対応が1つも取れませんでした")

    pitch_diffs = [est.notes[j].midi_note - truth.notes[i].midi_note for i, j in pairs]
    transpose = round(median(pitch_diffs))
    residuals = [d - transpose for d in pitch_diffs]
    pitch_acc = sum(1 for r in residuals if r == 0) / len(residuals)
    pitch_acc1 = sum(1 for r in residuals if abs(r) <= 1) / len(residuals)

    time_diffs = [est.notes[j].start_sec - truth.notes[i].start_sec for i, j in pairs]
    offset = median(time_diffs)
    time_res = [abs(d - offset) for d in time_diffs]
    time_res_sorted = sorted(time_res)

    return EvalResult(
        n_truth=len(truth.notes),
        n_est=len(est.notes),
        n_matched=len(pairs),
        coverage_truth=len(pairs) / len(truth.notes),
        transpose=transpose,
        pitch_acc=pitch_acc,
        pitch_acc_within1=pitch_acc1,
        time_offset_sec=offset,
        onset_mae_sec=sum(time_res) / len(time_res),
        onset_p90_sec=time_res_sorted[int(0.9 * (len(time_res_sorted) - 1))],
    )
