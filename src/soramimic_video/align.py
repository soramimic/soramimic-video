"""XFの行と元歌詞テキストのアライメント。

XF MIDIの歌詞(発音主体)を基本としつつ、字幕表示用に元歌詞の行を対応づける。
元歌詞のすべての行が歌われているとは限らず、逆にXF側にしかない行もありうるので、
単調(順序保存)なDPで各XF行に「元歌詞の1行 or 対応なし」を割り当てる。
1つの元歌詞行が連続する複数のXF行に対応するのは許す(長い歌詞行が
カラオケ表示で分割されるケース)。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import jaconv

from .project import Project

# これ未満の類似度なら「対応なし」とする
MATCH_THRESHOLD = 0.35
# 元歌詞側の行を読み飛ばすときのペナルティ(1行あたり)
SKIP_PENALTY = 0.05

_STRIP_RE = re.compile(r"[\s、。,.!?!?・「」『』()()〜~-]")


def _normalize(text: str) -> str:
    """比較用の正規化: 記号除去+カタカナをひらがなに寄せる。"""
    return jaconv.kata2hira(_STRIP_RE.sub("", text))


def _containment(xf: str, lyric: str) -> float:
    """XF行が元歌詞行にどれだけ含まれているか(0..1)。"""
    if not xf or not lyric:
        return 0.0
    sm = SequenceMatcher(None, lyric, xf, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(xf)


def align_texts(xf_lines: list[str], lyric_lines: list[str]) -> list[int | None]:
    """各XF行に対応する元歌詞行のindex(対応なしはNone)を返す。

    対応は単調: 使われる元歌詞行のindexは非減少(同じ行の継続は可)。
    """
    n, m = len(xf_lines), len(lyric_lines)
    if n == 0 or m == 0:
        return [None] * n

    xf_norm = [_normalize(t) for t in xf_lines]
    lyr_norm = [_normalize(t) for t in lyric_lines]
    sim = [[_containment(x, y) for y in lyr_norm] for x in xf_norm]

    NEG = float("-inf")
    # dp[i][j+1] = XF行 0..i-1 を割り当て済みで、最後に使った元歌詞行が j
    # (j=-1 はまだ何も使っていない)ときの最大スコア
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    # back[i][j+1] = (前状態のj+1, このXF行の割り当て)
    back: list[list[tuple[int, int | None]]] = [[(0, None)] * (m + 1) for _ in range(n + 1)]

    for i in range(n):
        for jj in range(m + 1):
            if dp[i][jj] == NEG:
                continue
            j = jj - 1
            # 対応なし
            if dp[i][jj] > dp[i + 1][jj]:
                dp[i + 1][jj] = dp[i][jj]
                back[i + 1][jj] = (jj, None)
            # 元歌詞行 k(継続 j または前進 >j)に割り当て
            for k in range(max(j, 0), m):
                if k < j:
                    continue
                if sim[i][k] < MATCH_THRESHOLD:
                    continue
                skipped = max(0, k - j - 1) if j >= 0 else k
                score = dp[i][jj] + sim[i][k] - SKIP_PENALTY * skipped
                if score > dp[i + 1][k + 1]:
                    dp[i + 1][k + 1] = score
                    back[i + 1][k + 1] = (jj, k)

    # 最良の最終状態からバックトラック
    best_jj = max(range(m + 1), key=lambda jj: dp[n][jj])
    assignments: list[int | None] = [None] * n
    jj = best_jj
    for i in range(n, 0, -1):
        prev_jj, assigned = back[i][jj]
        assignments[i - 1] = assigned
        jj = prev_jj
    return assignments


def align_lines(project: Project, lyric_lines: list[str]) -> None:
    """project.lines の original_text を埋める(破壊的)。"""
    lyrics = [ln.strip() for ln in lyric_lines]
    lyrics = [ln for ln in lyrics if ln]
    xf_texts = [ln.xf_surface or ln.xf_kana for ln in project.lines]
    assignments = align_texts(xf_texts, lyrics)
    for line, a in zip(project.lines, assignments, strict=True):
        line.original_text = lyrics[a] if a is not None else None
