"""XFの行と元歌詞テキストのアライメント。

XF MIDIの歌詞(発音主体)を基本としつつ、字幕表示用に元歌詞の行を対応づける。
元歌詞のすべての行が歌われているとは限らず、逆にXF側にしかない行もありうるので、
単調(順序保存)なDPで各XF行に「元歌詞の1行 or 対応なし」を割り当てる。
1つの元歌詞行が連続する複数のXF行に対応するのは許す(長い歌詞行が
カラオケ表示で分割されるケース)。

類似度は表記どうしの比較に加えて、元歌詞行をカナ読みに変換した発音形どうしの
比較も取り、高い方を使う。XF側がカナしかない(表記を持たないMIDI・editor JSONの
phrases)場合でも、漢字率の高い行を取りこぼさないため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import jaconv

from .kana import normalize_long_vowels
from .project import Project

# これ未満の類似度なら「対応なし」とする
MATCH_THRESHOLD = 0.35
# 元歌詞側の行を読み飛ばすときのペナルティ(1行あたり)
SKIP_PENALTY = 0.05

_STRIP_RE = re.compile(r"[\s、。,.!?!?・「」『』()()〜~-]")


def _normalize(text: str) -> str:
    """比較用の正規化: 記号除去+カタカナをひらがなに寄せる。"""
    return jaconv.kata2hira(_STRIP_RE.sub("", text))


def _pron_normalize(text: str) -> str:
    """発音形どうしの比較用の正規化: 記号除去+長音のゆれを揃えてひらがなに寄せる。

    XFのカナはヨウニ/ヨーニどちらの表記もありうるので、読みエンジンの
    発音形(長音は「ー」)と突き合わせる前に normalize_long_vowels で揃える。
    """
    kata = jaconv.hira2kata(_STRIP_RE.sub("", text))
    return jaconv.kata2hira(normalize_long_vowels(kata))


def _readings(lines: list[str]) -> list[str | None]:
    """各行のカナ読み。読み変換が使えない環境(MeCab等なし)では全てNone。"""
    try:
        from .reading import text_to_kana
    except ImportError:
        return [None] * len(lines)
    out: list[str | None] = []
    for ln in lines:
        try:
            out.append(text_to_kana(ln) or None)
        except RuntimeError:
            return [None] * len(lines)
    return out


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
    # 元歌詞行の読み(発音形)との比較も取り、表記比較と高い方を採用する
    xf_pron = [_pron_normalize(t) for t in xf_lines]
    lyr_pron = [r and _pron_normalize(r) for r in _readings(lyric_lines)]
    sim = [
        [
            max(_containment(x, y), _containment(xp, yp) if yp else 0.0)
            for y, yp in zip(lyr_norm, lyr_pron, strict=True)
        ]
        for x, xp in zip(xf_norm, xf_pron, strict=True)
    ]

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


# ---- 字幕の表示粒度(granularity) ----
#
# 字幕は「行」または「フレーズ」の粒度で出せる。
#   original: "line"(元歌詞の行を通しで) / "phrase"(そのXF行に対応する部分文字列)
#   parody:   "phrase"(XF行ごとの替え歌) / "line"(同一元歌詞行の替え歌を連結)
# subtitle要素ごとに指定でき、未指定なら source 既定(下記)にフォールバックする。

GRANULARITIES = ("line", "phrase")
# source ごとの既定粒度(subtitle要素・override いずれも未指定のとき)
DEFAULT_GRANULARITY = {"parody": "phrase", "original": "line"}


def resolve_granularity(
    source: str, element_granularity: str | None, override: dict[str, str] | None = None
) -> str:
    """subtitle要素の指定 > override(Web UIの一括指定) > source既定 の順で粒度を決める。"""
    if element_granularity in GRANULARITIES:
        return element_granularity
    if override:
        g = override.get(source)
        if g in GRANULARITIES:
            return g
    return DEFAULT_GRANULARITY.get(source, "phrase")


def parse_granularity_override(spec: str | None) -> dict[str, str] | None:
    """"parody:line|original:phrase" 形式の指定を {source: granularity} に。

    空文字や不正なトークンは無視する。何も取れなければ None(=既定を使う)。
    Web UIの粒度セレクタ・APIパラメータからの受け口。
    """
    override: dict[str, str] = {}
    for part in (spec or "").split("|"):
        src, sep, g = part.strip().partition(":")
        if not sep:
            continue
        src, g = src.strip(), g.strip()
        if src in DEFAULT_GRANULARITY and g in GRANULARITIES:
            override[src] = g
    return override or None


def effective_granularities(subtitles, override: dict[str, str] | None) -> dict[str, str]:
    """source ごとの実効粒度 {source: granularity} を返す。

    プレビューは字幕を替え歌1本・元歌詞1本に畳んで表示するので、source ごとに
    1つの粒度に解決しておく(同一sourceの要素が複数あれば先頭の指定を代表とする)。
    """
    out: dict[str, str] = {}
    for src in DEFAULT_GRANULARITY:
        el = next((e for e in subtitles if e.source == src), None)
        out[src] = resolve_granularity(src, getattr(el, "granularity", None), override)
    return out


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """正規化文字列と、その各文字が元の text の何文字目かの対応表を返す。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if not _STRIP_RE.sub("", ch):  # 空白・記号は落とす(_normalize と同じ基準)
            continue
        chars.append(jaconv.kata2hira(ch))
        idx.append(i)
    return "".join(chars), idx


def _proportional_split(xf_texts: list[str], lyric_line: str) -> list[str]:
    """XF各行の(正規化後)文字数比で元歌詞行を按分して切り出す(フォールバック)。"""
    weights = [max(1, len(_normalize(x))) for x in xf_texts]
    total = sum(weights)
    n = len(xf_texts)
    length = len(lyric_line)
    cuts = [0]
    acc = 0
    for w in weights[:-1]:
        acc += w
        # 直前の cut より必ず1文字は進め、末尾に n-1 文字は残す(空文字を作らない)
        pos = round(length * acc / total)
        pos = max(cuts[-1] + 1, min(pos, length - (n - len(cuts))))
        cuts.append(pos)
    cuts.append(length)
    return [lyric_line[cuts[i]:cuts[i + 1]].strip() or lyric_line[cuts[i]:cuts[i + 1]]
            for i in range(n)]


def split_lyric_to_phrases(xf_texts: list[str], lyric_line: str) -> list[str]:
    """同一元歌詞行に割り当てられた連続XF行それぞれに対応する部分文字列を返す。

    SequenceMatcher の一致区間で元歌詞行を順に切り分ける。XF側がカナのみで
    漢字の元歌詞と表記が重ならない等、一致が取れない/曖昧なときは文字数比の
    按分にフォールバックする。返り値は xf_texts と同数で、連結すると(空白等の
    正規化差を除き)元の行に一致し、どれも空文字にならない。
    """
    n = len(xf_texts)
    if n == 0:
        return []
    if n == 1:
        return [lyric_line]
    norm_lyric, lyr_map = _norm_with_map(lyric_line)
    if not norm_lyric:
        return _proportional_split(xf_texts, lyric_line)

    # 元歌詞(正規化)をXF行の順にカーソルを進めながら切っていく
    cursor = 0  # norm_lyric 上の現在位置
    cut_norm = [0]  # 各XF行の開始位置(norm_lyric座標)
    ok = True
    for xf in xf_texts[:-1]:
        nx = _normalize(xf)
        if not nx:
            ok = False
            break
        sub = norm_lyric[cursor:]
        sm = SequenceMatcher(None, sub, nx, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        matched = sum(b.size for b in blocks)
        # このXF行が元歌詞側とほとんど重ならない → 全体を按分に切り替える
        if not blocks or matched < len(nx) * 0.5:
            ok = False
            break
        end_in_sub = blocks[-1].a + blocks[-1].size  # sub 上でのこの行の末尾
        new_cursor = cursor + end_in_sub
        # 単調に進み、残りのXF行ぶんは必ず1文字以上残す
        if new_cursor <= cursor or new_cursor > len(norm_lyric) - (n - len(cut_norm)):
            ok = False
            break
        cut_norm.append(new_cursor)
        cursor = new_cursor
    if not ok:
        return _proportional_split(xf_texts, lyric_line)

    # norm 座標の切れ目を元歌詞の文字位置に写して切り出す(間の空白は次の行に付く)
    bounds = [0]
    for c in cut_norm[1:]:
        bounds.append(lyr_map[c])
    bounds.append(len(lyric_line))
    pieces = [lyric_line[bounds[i]:bounds[i + 1]].strip() for i in range(n)]
    if any(not p for p in pieces):  # 念のため(空文字が出たら按分に退避)
        return _proportional_split(xf_texts, lyric_line)
    return pieces


@dataclass
class SubtitleSegment:
    """字幕1枚ぶんの表示テキストと区間。indices は元の行インデックス(複数=マージ)。"""

    text: str
    start: float
    end: float
    indices: list[int] = field(default_factory=list)


def _group_by_original(originals: list[str | None]) -> list[tuple[int, int]]:
    """連続する同一元歌詞行を [start, end) グループにまとめる。None は隣と結合しない。"""
    groups: list[tuple[int, int]] = []
    i, n = 0, len(originals)
    while i < n:
        j = i + 1
        if originals[i] is not None:
            while j < n and originals[j] == originals[i]:
                j += 1
        groups.append((i, j))
        i = j
    return groups


def build_subtitle_segments(
    kind: str,
    granularity: str,
    originals: list[str | None],
    full_texts: list[str],
    xf_texts: list[str],
    spans: list[tuple[float, float]],
    sep: str = "  ",
) -> list[SubtitleSegment]:
    """粒度に応じた字幕セグメント列を作る(video/preview 共通)。

    - originals: 各行に対応づいた元歌詞の行(未対応は None)。グループ化のキー。
    - full_texts: 各行の「行粒度」表示テキスト。original はフォールバック込みの表示文、
      parody は単語 surface を連結した行の替え歌。
    - xf_texts: 各行のXF表記(元歌詞のフレーズ切り出しに使う)。
    - spans: 各行の表示区間 [start, end]。
    """
    segments: list[SubtitleSegment] = []
    for a, b in _group_by_original(originals):
        idxs = list(range(a, b))
        if granularity == "line":
            # グループを1枚に畳む(通しタイミング=チラつき防止/替え歌の行連結)
            if kind == "parody":
                text = sep.join(full_texts[k] for k in idxs if full_texts[k])
            else:
                text = full_texts[a]  # 元歌詞行はグループ内で同一
            segments.append(SubtitleSegment(text, spans[a][0], spans[b - 1][1], idxs))
        else:  # phrase
            lyric_line = originals[a]
            if kind == "original" and lyric_line is not None and (b - a) > 1:
                pieces = split_lyric_to_phrases([xf_texts[k] for k in idxs], lyric_line)
            else:
                pieces = [full_texts[k] for k in idxs]
            for k, piece in zip(idxs, pieces, strict=True):
                segments.append(SubtitleSegment(piece, spans[k][0], spans[k][1], [k]))
    return segments


def segment_text_by_line(segments: list[SubtitleSegment], n: int) -> list[str]:
    """行インデックス→その行が属するセグメントの表示テキスト(プレビュー用)。"""
    out = [""] * n
    for seg in segments:
        for k in seg.indices:
            if 0 <= k < n:
                out[k] = seg.text
    return out
