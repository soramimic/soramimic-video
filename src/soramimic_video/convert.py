"""替え歌変換ステージ: soramimic ライブラリで行ごとの替え歌単語列を得る。

変換入力はXFの読み(カナ)を行ごとに連結した文字列。変換結果の period は
変換エンジンが返すユニット列(mora単位)へのindexなので、
ユニットの文字オフセット → XFモーラ(音符)の文字オフセット の対応で
各単語を音符ID列に写像する。
"""

from __future__ import annotations

import csv
import difflib
import logging
import re
from pathlib import Path
from typing import Any

from .kana import split_fine_moras, split_moras
from .project import Parody, ParodyLine, ParodyWord, Project
from .soramimic_engine import run_convert

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORDLISTS_DIR = REPO_ROOT / "external" / "soramimic-wordlists"

# editor(conf/setting.json)と同じ既定の絞り込み
DEFAULT_WHERE = {
    "baseball": "type=family or type=registered or type=full",
    "football": "type=family or type=registered or type=full",
}


def resolve_wordlist(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".csv" and p.exists():
        return p
    candidate = WORDLISTS_DIR / f"{name_or_path}.csv"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"単語リストが見つかりません: {name_or_path} "
        f"(external/soramimic-wordlists のリスト名かCSVパスを指定してください)"
    )


def parse_convert_params(spec: str | None) -> dict[str, str]:
    """"KEY=VALUE" を並べた文字列を {KEY: VALUE} に分解する。

    Web UI・API から変換エンジンのパラメータ(DUPLICATE など)を受け取る入口。
    区切りは改行・セミコロン・縦棒のいずれか。'=' を含まない要素や空キーは無視する
    (値の型変換 bool/int/float は convert_project 内の _coerce_params が行う)。
    CLI の ``--param KEY=VALUE`` と同じ意味のパラメータを渡せる。
    """
    out: dict[str, str] = {}
    for part in re.split(r"[\n;|]", spec or ""):
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and key:
            out[key] = value.strip()
    return out


def _coerce_params(params: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, str) and v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
            continue
        try:
            out[k] = int(v)
        except (ValueError, TypeError):
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                out[k] = v
    return out


def _offset_map(src: str, dst: str) -> list[int]:
    """srcの各文字オフセット(0..len(src))をdstのオフセットに写す表。

    完全一致なら恒等。差異があればdifflibで最善の対応を取る。
    """
    if src == dst:
        return list(range(len(src) + 1))
    table = [0] * (len(src) + 1)
    sm = difflib.SequenceMatcher(None, src, dst, autojunk=False)
    last_dst = 0
    for a, b, size in sm.get_matching_blocks():
        for i in range(a, a + size + 1):
            table[i] = b + (i - a)
        if size:
            last_dst = b + size
        # マッチしない区間は直前のdst位置を引き継ぐ(単調性を保つ)
        for i in range(a + size + 1, len(src) + 1):
            table[i] = max(table[i], last_dst)
    table[len(src)] = max(table[len(src)], len(dst))
    return table


# 変換のバリエーション分割で直前の音節に吸収されうるモーラ
# (撥音・促音・長音・母音字。例: フェ+ン→フェー, テ+イ→テー, ヨ+ウ→ヨー)
_ABSORBABLE = set("ンッーアイウエオ")


def _compressed_moras_per_element(
    word_kana: str, pronunciation: list[str]
) -> list[list[str]] | None:
    """各発音要素が圧縮で「ー」に潰した単語モーラ(ン・ッ・母音字)を求める。

    変換エンジンの getVariation は単語の1音節を ``[頭] + ー``(撥音等を長音に
    圧縮)という要素にすることがある(例: 単語リン→要素「リー」)。この関数は
    単語kanaのfineモーラ列と発音要素を突き合わせ、各要素の末尾「ー」が単語側の
    どのモーラ由来かを求める。末尾「ー」が単語自身の長音(単語側もfineモーラが
    「ー」)なら圧縮ではないので除外する(例: 単語ハビー→要素「ビー」は圧縮なし)。
    整合が取れなければ None(呼び出し側は復元せず現状動作)。
    """
    fines = split_fine_moras(word_kana)
    wi = 0
    out: list[list[str]] = []
    for p in pronunciation:
        head = p.rstrip("ー")
        nlong = len(p) - len(head)
        acc = ""
        while wi < len(fines) and len(acc) < len(head):
            acc += fines[wi]
            wi += 1
        if acc != head:
            return None
        comp: list[str] = []
        for _ in range(nlong):
            if wi >= len(fines) or fines[wi] not in _ABSORBABLE:
                return None
            m = fines[wi]
            wi += 1
            if m != "ー":  # 単語自身の長音は圧縮ではない
                comp.append(m)
        out.append(comp)
    if wi != len(fines):
        return None
    return out


# --- 母音一致優先の単語内アライメント(歌唱タイミングの自然化) ---
# soramimic 本体の音素定義(char_to_vowel/char_to_consonant)を再利用し、母音一致=
# 第1キー・子音一致=第2キーの辞書順スコアで、単語のモーラをユニット境界内の音符へ
# 割り当てる(生の類似度テーブルは使わない)。母音を最優先で最大化するため、音符集合
# を変えずに「どのモーラがどの音符に載るか/どの音符が継続ーになるか」だけが変わる。

_kana_phon_fns: Any = None


def _kana_phon() -> Any:
    """soramimic の母音・子音抽出関数を遅延取得してキャッシュする。"""
    global _kana_phon_fns
    if _kana_phon_fns is None:
        from soramimic.kana_to_syllable import char_to_consonant, char_to_vowel

        _kana_phon_fns = (char_to_vowel, char_to_consonant)
    return _kana_phon_fns


def _rep_mora(kana: str) -> str:
    """複数モーラを含むkana(複合音符など)の音素代表として先頭モーラを取る。"""
    moras = split_moras(kana)
    return moras[0] if moras else kana


# 歌唱で脱落・長音化しやすいモーラ(促音・撥音・長音)
_DROPOUT_MORA = {"ッ", "ン", "ー"}

# エイ型・オウ型連鎖で脱落・長音化しやすいと見なせる2モーラ目は、母音単独のかな
# (イ/ウ そのもの)だけ。ディ・キ・リ(子音付きi段)やク・ル(子音付きu段)は直前が
# e段/o段でも独立モーラとして発音されるので対象外。判定は「母音がi/uか」ではなく
# 「かなが イ/ウ そのものか」で行う。値は連鎖成立に必要な直前モーラの母音。
_CHAIN_SECOND_VOWEL = {"イ": "エ", "ウ": "オ"}


def _dropout_flags(moras: list[str]) -> list[bool]:
    """各モーラが脱落・長音化しやすいか。特殊モーラ(ッ/ン/ー)と、母音単独の
    イ/ウ が直前 e段/o段に続くエイ/オウ型連鎖の2モーラ目(例: ケ+イ、コ+ウ)を
    True にする。子音付きのi/u段モーラ(ディ・キ・ク・ル等)は対象外。"""
    char_to_vowel, _ = _kana_phon()
    flags: list[bool] = []
    for i, m in enumerate(moras):
        drop = m in _DROPOUT_MORA
        if not drop and i > 0 and m in _CHAIN_SECOND_VOWEL:
            pv = char_to_vowel(_rep_mora(moras[i - 1]))
            drop = pv == _CHAIN_SECOND_VOWEL[m]
        flags.append(drop)
    return flags


def _pair_score(
    elem_head: str, note_kana: str, elem_drop: bool, note_drop: bool
) -> int:
    """替え歌モーラと元音符kanaのペアスコア(辞書順: 母音一致>子音一致>脱落調整)。

    母音一致(重み1000)は子音一致(10)・脱落調整(<=2)の総和より必ず大きいので、
    母音一致数を最優先で最大化する(=母音一致率が悪化しない)。脱落調整は、
    脱落しやすい要素/音符ほど実音の載せ先としての優先度を下げる同点の微調整。
    """
    char_to_vowel, char_to_consonant = _kana_phon()
    er, nr = _rep_mora(elem_head), _rep_mora(note_kana)
    vowel = 1 if char_to_vowel(er) == char_to_vowel(nr) else 0
    cons = 1 if char_to_consonant(er) == char_to_consonant(nr) else 0
    tie = (0 if note_drop else 1) + (0 if elem_drop else 1)
    return 1000 * vowel + 10 * cons + tie


def _align_positions(
    scores: list[list[int]],
    eu: int,
    k: int,
    force_first: bool,
    adj: list[bool] | None = None,
) -> list[int] | None:
    """eu 個の要素を k 個の音符(0..k-1)へ、順序を保った単調増加の位置列で割り当て、
    スコア総和を最大化する(DP)。同点は前方の位置を優先。force_first のとき先頭要素は
    音符0に固定(語頭の継続ー化を避ける)。余った音符は空(=継続ー)になる。eu<=k 前提。

    adj[j]=True の要素は直前要素と隣接音符(pos[j]=pos[j-1]+1)に固定する(促音ッの
    閉音節ハード制約)。制約を満たす配置が無ければ None を返す。
    """
    neg = float("-inf")
    dp = [[neg] * k for _ in range(eu)]
    par = [[-1] * k for _ in range(eu)]
    for kk in range(k):
        if force_first and kk != 0:
            continue
        if kk <= k - eu:  # 後続 eu-1 個が入る余地が要る
            dp[0][kk] = scores[0][kk]
    for j in range(1, eu):
        must_adj = adj is not None and adj[j]
        for kk in range(j, k):  # 要素jは最短でも位置j
            if must_adj:  # 直前要素の直後(pos[j-1]=kk-1)のみ許す
                kp = kk - 1
                if kp >= j - 1 and dp[j - 1][kp] > neg:
                    dp[j][kk] = dp[j - 1][kp] + scores[j][kk]
                    par[j][kk] = kp
                continue
            best, bpar = neg, -1
            for kp in range(j - 1, kk):
                if dp[j - 1][kp] > best:  # 同点は小さいkp(前方)を保持
                    best, bpar = dp[j - 1][kp], kp
            if best > neg:
                dp[j][kk] = best + scores[j][kk]
                par[j][kk] = bpar
    best_k, best_v = -1, neg
    for kk in range(eu - 1, k):
        if dp[eu - 1][kk] > best_v:  # 同点は小さいkk(前方)を保持
            best_v, best_k = dp[eu - 1][kk], kk
    if best_k < 0:  # 制約を満たす配置なし
        return None
    pos = [0] * eu
    kk = best_k
    for j in range(eu - 1, -1, -1):
        pos[j] = kk
        kk = par[j][kk]
    return pos


# --- 要素→音節(ユニット)への個数配分を最適化する外側DP ---
# 音節対応(unit_note_ks)は維持したまま、各音節に何個の要素を載せるか(e[u])だけを、
# 音節内配置(内側=_align_positions)スコアの総和が最大になるよう選ぶ。ホソウラ等で
# 位置ベース配分が誤って隣音節に要素を寄せる問題を解く。


def _positional_distribution(n_pron: int, unit_note_ks: list[list[int]]) -> list[int]:
    """従来の位置ベース配分: 各音節に最低1、余剰は空きのある音節へ左から。"""
    n_units = len(unit_note_ks)
    e = [0] * n_units
    remaining = n_pron
    for u in range(n_units):
        if remaining <= 0:
            break
        e[u] = 1
        remaining -= 1
    u = 0
    while remaining > 0 and u < n_units:
        spare = max(0, len(unit_note_ks[u]) - e[u])
        take = min(spare, remaining)
        e[u] += take
        remaining -= take
        u += 1
    if remaining > 0 and n_units:  # 音符数を超える要素は末尾ユニットに寄せる
        e[-1] += remaining
    return e


# 替え歌側の促音ッは直前モーラと閉音節を成し不可分(隣接音符ハード制約)
_SOKUON = "ッ"

# --- 溢れ(要素数>音符数)時の連続分割DP ---
# 要素数が音符数を超えると従来は配分DPを諦めて余剰を末尾音符へ寄せていた
# (ラグナット→ラ|グ|ナ|ット)。代わりに要素列を音符数個の連続非空区間に分割し、
# 各音符へ順に載せる分割をDPで選ぶ(全音符が実音を持ち、促音ッが音符頭に来ない)。
# 区間先頭要素は _pair_score(母音一致優先)、2要素目以降は「脱落系モーラ
# (ッ/ン/ー/エイ・オウ連鎖のイ・ウ)」や「元音符が複数モーラ(ダッ/テイ等の
# 閉音節・長音)」ほど載せやすいボーナスで評価する。

# 脱落系モーラを同一音符へ積む優先ボーナス。母音一致(1000)より小さくし、
# 母音一致数を犠牲にしてまで脱落系ペアを作らない。
_STACK_BONUS = 200
# 元音符が複数モーラ(閉音節・長音)のとき、その音符へ積む追加ボーナス
_STACK_NOTE_BONUS = 100


def _overflow_alloc(
    pronunciation: list[str],
    heads: list[str],
    elem_drop: list[bool],
    note_drop: list[bool],
    id_kanas: list[str],
) -> list[str] | None:
    """溢れ時の音符ごとの歌唱カナ。要素列を音符数個の連続非空区間へ分割する。

    dp[j][t] = 先頭 j 要素を先頭 t 音符に割り当てた最良スコア。
    区間が促音ッで始まる分割は閉音節ペアの分断なので不可。同点は従来動作
    (末尾音符へ寄せる=前方の音符ほど区間を短く)に寄せる。
    実行可能な分割がなければ None(従来の左詰めへフォールバック)。
    """
    n, k = len(pronunciation), len(id_kanas)
    if n <= k or k == 0:
        return None

    def seg_score(a: int, b: int, t: int) -> int | None:
        """要素 [a,b) を音符 t に載せるスコア。ッ頭は不可。"""
        if heads[a].startswith(_SOKUON):
            return None
        s = _pair_score(heads[a], id_kanas[t], elem_drop[a], note_drop[t])
        note_multi = len(split_moras(id_kanas[t])) >= 2
        for j in range(a + 1, b):
            if elem_drop[j]:
                s += _STACK_BONUS
            if note_multi:
                s += _STACK_NOTE_BONUS
        return s

    neg = float("-inf")
    dp = [[neg] * (k + 1) for _ in range(n + 1)]
    par = [[-1] * (k + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for t in range(1, k + 1):
        for j in range(t, n - (k - t) + 1):  # 残り音符ぶんの要素を残す
            for a in range(t - 1, j):  # 音符 t-1 に要素 [a, j)
                if dp[a][t - 1] == neg:
                    continue
                seg = seg_score(a, j, t - 1)
                if seg is None:
                    continue
                cand = dp[a][t - 1] + seg
                # 同点は大きい a(=前方の音符ほど短い区間=従来の末尾寄せ)を採用
                if cand >= dp[j][t]:
                    dp[j][t] = cand
                    par[j][t] = a
    if dp[n][k] == neg:
        return None
    bounds = [n]
    j = n
    for t in range(k, 0, -1):
        j = par[j][t]
        bounds.append(j)
    bounds.reverse()
    return [
        "".join(pronunciation[bounds[t] : bounds[t + 1]]) for t in range(k)
    ]


def _paired_sokuon(heads: list[str]) -> list[bool]:
    """替え歌側の促音ッが直前モーラと閉音節を成す(不可分)位置を True にする。
    語頭ッ・ッッ連続・語末ッは安全側で対象外(従来配置にフォールバック)。"""
    n = len(heads)
    paired = [False] * n
    for j in range(1, n - 1):  # 語末(n-1)は対象外
        if heads[j] == _SOKUON and heads[j - 1] not in ("", "ー", _SOKUON):
            paired[j] = True
    return paired


def _inner_positions(
    heads: list[str],
    id_kanas: list[str] | None,
    elem_drop: list[bool] | None,
    note_drop: list[bool] | None,
    ks: list[int],
    base: int,
    eu: int,
    force_first: bool,
    adj: list[bool] | None = None,
) -> list[int] | None:
    """音節内(内側): base..base+eu の要素を ks 音符へ載せる位置(ks内index)を返す。
    notes_kana があり音符数>要素数のときだけ母音一致優先DP、それ以外は左詰め。
    adj[l]=True(促音ッの閉音節)は直前要素と隣接に固定。制約充足不能なら None。"""
    if not ks or eu <= 0:
        return []
    k = len(ks)
    adj_needed = adj is not None and any(adj)
    if eu >= k:  # 1音符1要素(eu==k、隣接自明)または溢れ(eu>k)は左詰め
        if eu > k and adj_needed:
            return None  # 溢れではペアの隣接を保証できない
        return [min(j, k - 1) for j in range(eu)]
    if id_kanas is not None:
        assert elem_drop is not None and note_drop is not None
        svec = [
            [
                _pair_score(
                    heads[base + j], id_kanas[ks[kk]],
                    elem_drop[base + j], note_drop[ks[kk]],
                )
                for kk in range(k)
            ]
            for j in range(eu)
        ]
        return _align_positions(svec, eu, k, force_first, adj)
    return [min(j, k - 1) for j in range(eu)]  # notes_kana無し: 左詰め(連続=隣接OK)


def _seg_score(
    heads: list[str],
    id_kanas: list[str] | None,
    elem_drop: list[bool] | None,
    note_drop: list[bool] | None,
    unit_note_ks: list[list[int]],
    u: int,
    start: int,
    length: int,
    paired: list[bool] | None = None,
) -> int | None:
    """音節 u に要素 [start, start+length) を載せたときの内側配置スコア合計。
    促音ッのペアが分断(区間先頭がッ)・隣接不能な区間は制約違反として None を返す。"""
    if length <= 0:
        return 0
    if paired is not None and paired[start]:
        return None  # 区間先頭がペア後半(ッ)= ペア分断
    ks = unit_note_ks[u]
    if id_kanas is None or not ks:
        return 0
    assert elem_drop is not None and note_drop is not None
    adj = [paired[start + j] for j in range(length)] if paired is not None else None
    pos = _inner_positions(
        heads, id_kanas, elem_drop, note_drop, ks, start, length,
        force_first=(start == 0), adj=adj,
    )
    if pos is None:  # 隣接制約を満たせない
        return None
    s = 0
    for j in range(length):
        if j < len(pos):
            k = ks[pos[j]]
            s += _pair_score(
                heads[start + j], id_kanas[k], elem_drop[start + j], note_drop[k]
            )
    return s


def _distribution_score(
    e: list[int],
    heads: list[str],
    id_kanas: list[str] | None,
    elem_drop: list[bool] | None,
    note_drop: list[bool] | None,
    unit_note_ks: list[list[int]],
    paired: list[bool] | None = None,
) -> int | None:
    """個数配分 e の内側配置スコア合計。制約違反を含むなら None。"""
    s = 0
    start = 0
    for u, cnt in enumerate(e):
        seg = _seg_score(
            heads, id_kanas, elem_drop, note_drop, unit_note_ks, u, start, cnt, paired
        )
        if seg is None:
            return None
        s += seg
        start += cnt
    return s


def _distribute_moras(
    heads: list[str],
    id_kanas: list[str],
    elem_drop: list[bool],
    note_drop: list[bool],
    unit_note_ks: list[list[int]],
    n_pron: int,
    paired: list[bool] | None = None,
) -> tuple[list[int] | None, int]:
    """外側DP: 要素列を順序保持で音節へ連続割り当てする個数配分 e[] を、内側配置
    スコアの総和が最大になるよう選ぶ。各音節の要素数はその音符数まで(1音符1実音)。
    促音ッの閉音節ペアは分断・隣接不能な区間を除外(ハード制約)。(e_opt, 総スコア)
    を返す。到達不能なら (None, -inf)。前提: n_pron <= sum(音符数)。
    """
    n_units = len(unit_note_ks)
    # 位置ベース配分の累積境界(同点時に現行配分へ寄せるタイブレークの基準)
    cum_pos = [0] * (n_units + 1)
    for u, c in enumerate(_positional_distribution(n_pron, unit_note_ks)):
        cum_pos[u + 1] = cum_pos[u] + c
    neg = -(10**18)
    # dp[u][i] = (スコア, 位置配分の累積一致数)。スコア優先、同点は一致数が多い方。
    dp = [[(neg, 0)] * (n_pron + 1) for _ in range(n_units + 1)]
    par = [[-1] * (n_pron + 1) for _ in range(n_units + 1)]
    dp[0][0] = (0, 0)
    for u in range(1, n_units + 1):
        cap = len(unit_note_ks[u - 1])
        for i in range(n_pron + 1):
            # 語頭音節(音符あり)は空にしない=語頭の継続ー化を防ぐ(語頭固定)
            if u == 1 and cap > 0 and i == 0 and n_pron > 0:
                continue
            match_i = 1 if i == cum_pos[u] else 0  # 累積境界が現行配分と一致
            for j in range(max(0, i - cap), i + 1):  # 音節 u-1 に [j,i)、要素数<=cap
                prev = dp[u - 1][j]
                if prev[0] == neg:
                    continue
                seg = _seg_score(
                    heads, id_kanas, elem_drop, note_drop, unit_note_ks,
                    u - 1, j, i - j, paired,
                )
                if seg is None:  # ペア制約違反の区間は不可
                    continue
                cand = (prev[0] + seg, prev[1] + match_i)
                if cand > dp[u][i]:
                    dp[u][i] = cand
                    par[u][i] = j
    if dp[n_units][n_pron][0] == neg:
        return None, neg
    e_opt = [0] * n_units
    i = n_pron
    for u in range(n_units, 0, -1):
        j = par[u][i]
        e_opt[u - 1] = i - j
        i = j
    return e_opt, dp[n_units][n_pron][0]


def _map_word_to_notes(
    unit_lens: list[int],
    note_lens: list[int],
    offset_map: list[int],
    period: tuple[int, int],
    pronunciation: list[str] | None = None,
    word_kana: str = "",
    notes_kana: list[str] | None = None,
) -> tuple[list[int], list[str]]:
    """periodユニット区間 → 重なる音符indexの列と音符ごとの歌唱カナ。

    発音要素(pronunciation)は変換エンジンが period 内の元歌詞ユニット
    (units=音節)のバリエーション(syllableToVariation)と要素数を揃えて
    マッチさせた結果なので、要素は「元歌詞ユニット」を単位に音符へ載る。
    ユニット境界を尊重して要素を音符へ配置し、さらに単語側の圧縮モーラ
    (撥音等)を同ユニット内の空き音符へ復元する。

    notes_kana(行の音符ごとの元kana、note_lens と並行)を渡すと、ユニット内で
    要素をどの音符に載せるか(=どの音符を継続ーにするか)を母音一致優先のDPで
    決める。ユニット境界はハード制約で、音符集合(ids)は変わらない。
    """
    unit_cum = [0]
    for length in unit_lens:
        unit_cum.append(unit_cum[-1] + length)
    start_src = unit_cum[period[0]]
    end_src = unit_cum[period[1]]
    start_c = offset_map[start_src]
    end_c = offset_map[end_src]

    note_cum = [0]
    for length in note_lens:
        note_cum.append(note_cum[-1] + length)
    ids = [
        i
        for i in range(len(note_lens))
        if note_cum[i] < end_c and note_cum[i + 1] > start_c
    ]

    kana_per_note = [""] * len(ids)
    if not pronunciation:
        return ids, kana_per_note

    # 各ユニット(元歌詞音節)が占める ids 内の音符位置を求める
    units = list(range(period[0], period[1]))
    unit_note_ks: list[list[int]] = []
    for u in units:
        lo, hi = offset_map[unit_cum[u]], offset_map[unit_cum[u + 1]]
        if hi <= lo:  # 対応先の文字がない(脱落): 直近の音符に寄せる
            lo, hi = max(0, lo - 1), lo
        ks = [k for k, i in enumerate(ids) if note_cum[i] < hi and note_cum[i + 1] > lo]
        unit_note_ks.append(ks)

    comp_per_elem = (
        _compressed_moras_per_element(word_kana, pronunciation) if word_kana else None
    )
    # 復元先にできるのは1ユニットだけが占有する音符(共有音符は避ける)
    owners = [0] * len(ids)
    for ks in unit_note_ks:
        for k in ks:
            owners[k] += 1

    # 母音一致優先アライメント用の下準備(notes_kana が渡されたときのみ)
    heads = [p.rstrip("ー") for p in pronunciation]
    if notes_kana is not None:
        id_kanas: list[str] | None = [notes_kana[i] for i in ids]
        elem_drop: list[bool] | None = _dropout_flags(heads)
        note_drop: list[bool] | None = _dropout_flags(id_kanas)  # type: ignore[arg-type]
    else:
        id_kanas = elem_drop = note_drop = None

    # 替え歌側の促音ッ+直前モーラの閉音節ペア(不可分・隣接音符ハード制約)
    paired = _paired_sokuon(heads) if notes_kana is not None else None

    # 溢れ(要素数>音符数)は、要素列を音符数個の連続区間に分割するDPで
    # 音符ごとの歌唱カナを直接決める(従来は末尾音符へ丸ごと寄せていた)。
    # 全音符が実音を持つのでユニット内の空き音符への圧縮復元は不要。
    if id_kanas is not None and len(pronunciation) > len(ids):
        assert elem_drop is not None and note_drop is not None
        alloc = _overflow_alloc(pronunciation, heads, elem_drop, note_drop, id_kanas)
        if alloc is not None:
            return ids, alloc

    # 要素→音節(ユニット)の個数配分。既定は従来の位置ベース。notes_kana があり溢れ
    # (要素数>音符数)でなければ、音節内配置スコア総和を最大化する外側DPで最適化する
    # (音節対応は維持、促音ッの閉音節ペアは分断しない、同点は現行配分)。
    n_units = len(units)
    e = _positional_distribution(len(pronunciation), unit_note_ks)
    if id_kanas is not None and n_units and len(pronunciation) <= len(ids):
        e_opt, opt_score = _distribute_moras(
            heads, id_kanas, elem_drop, note_drop,  # type: ignore[arg-type]
            unit_note_ks, len(pronunciation), paired,
        )
        pos_score = _distribution_score(
            e, heads, id_kanas, elem_drop, note_drop, unit_note_ks, paired
        )
        # e_opt が制約を満たし、かつ現行配分より良い(または現行が制約違反)なら採用
        if e_opt is not None and (pos_score is None or opt_score > pos_score):
            e = e_opt

    base = 0
    for ui in range(n_units):
        eu = e[ui]
        ks = unit_note_ks[ui]
        # 音節内(内側)で eu 要素を ks 音符へ載せる位置を決める(母音一致優先DPまたは
        # 左詰め)。ユニット境界(ks)はハード制約。語頭要素は先頭音符に固定。促音ッの
        # 閉音節ペアは直前モーラと隣接音符へ固定する。
        adj_local = (
            [paired[base + j] for j in range(eu)] if paired is not None else None
        )
        pos = _inner_positions(
            heads, id_kanas, elem_drop, note_drop, ks, base, eu,
            force_first=(base == 0 and eu > 0), adj=adj_local,
        )
        if pos is None:  # 制約充足不能(フォールバック): 従来の左詰め
            pos = [min(j, len(ks) - 1) for j in range(eu)] if ks else []
        for j in range(eu):
            p = pronunciation[base + j]
            if not ks:
                continue
            k = ks[pos[j]]
            is_last = j == eu - 1
            comp = comp_per_elem[base + j] if (is_last and comp_per_elem) else []
            if comp:
                trailing = [
                    kk
                    for kk in ks[pos[j] + 1 :]
                    if kana_per_note[kk] == "" and owners[kk] == 1
                ]
                r = min(len(comp), len(trailing))
                head = p.rstrip("ー")
                genuine = (len(p) - len(head)) - len(comp)  # 単語自身の長音ぶんのー
                kana_per_note[k] += head + "ー" * (genuine + (len(comp) - r))
                for x in range(r):
                    kana_per_note[trailing[x]] = comp[x]
            else:
                kana_per_note[k] += p
        base += eu
    return ids, kana_per_note


def _word_char_span(
    unit_lens: list[int], offset_map: list[int], period: tuple[int, int]
) -> tuple[int, int]:
    """単語(period)が占める音符側の文字オフセット区間 [start_c, end_c)。"""
    unit_cum = [0]
    for length in unit_lens:
        unit_cum.append(unit_cum[-1] + length)
    return offset_map[unit_cum[period[0]]], offset_map[unit_cum[period[1]]]


def _resolve_shared_notes(
    pending: list[list[Any]], note_cum: list[int]
) -> list[tuple[int, int, int]]:
    """複合音符が単語境界を跨いで二重割り当てされたのを解消する(破壊的)。

    原曲側の1音符に元歌詞かなが複数文字入る複合音符(タイ・継続音由来)が
    替え歌の単語境界を横切ると、_map_word_to_notes は独立処理のため両単語に
    その音符を割り当てる。合成時 lyric_map は後勝ち上書きなので、先行単語の
    末尾モーラが後続単語に潰される。ここで同一音符は**文字オーバーラップが
    大きい方の単語**(同点なら先行単語)に一本化し、外れた側からは音符と
    対応する歌唱カナを対で取り除く(note_ids と note_kana の同長を保つ)。

    pending の各要素は [word, note_idx, note_kana, start_c, end_c](破壊的に更新)。
    解消した衝突の (note位置index, 勝った単語index, 負けた単語index) を返す。
    """
    holders: dict[int, list[tuple[int, int]]] = {}
    for wi, (_word, note_idx, _kana, start_c, end_c) in enumerate(pending):
        for i in note_idx:
            overlap = min(end_c, note_cum[i + 1]) - max(start_c, note_cum[i])
            holders.setdefault(i, []).append((wi, overlap))

    resolved: list[tuple[int, int, int]] = []
    to_drop: dict[int, set[int]] = {}
    for i, claims in holders.items():
        if len(claims) <= 1:
            continue
        # 文字オーバーラップ最大の単語が音符を取る。同点は index が小さい先行単語。
        winner = max(claims, key=lambda c: (c[1], -c[0]))[0]
        for wi, _overlap in claims:
            if wi != winner:
                to_drop.setdefault(wi, set()).add(i)
                resolved.append((i, winner, wi))

    for wi, drop in to_drop.items():
        _word, note_idx, note_kana, start_c, end_c = pending[wi]
        kept = [
            (i, k) for i, k in zip(note_idx, note_kana, strict=True) if i not in drop
        ]
        pending[wi][1] = [i for i, _ in kept]
        pending[wi][2] = [k for _, k in kept]
    return resolved


def _load_wordlist_rows(csv_path: Path) -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row.get("id", ""), []).append(row)
    return rows


def _find_row(
    rows_by_id: dict[str, list[dict[str, str]]], word: dict
) -> dict[str, str] | None:
    rows = rows_by_id.get(str(word.get("id", "")))
    if not rows:
        return None
    for row in rows:
        if row.get("surface") == word.get("surface"):
            return row
    return rows[0]


def convert_project(
    project: Project,
    wordlist: str,
    where: str | None = None,
    params: dict[str, str] | None = None,
) -> dict:
    """project.parody を埋める(破壊的)。変換エンジンの生の応答を返す。

    生の応答(units・period付きの単語列・tokensList)は editor 連携の
    書き出しに必要なので、呼び出し側でプロジェクトディレクトリに保存する。
    """
    csv_path = resolve_wordlist(wordlist)
    if where is None:
        where = DEFAULT_WHERE.get(csv_path.stem)
    coerced = _coerce_params(params or {})
    # エンジン既定はDUPLICATE:true(単語重複あり)だが、本家Web UIの既定は
    # 「なし」。未指定時はWeb UIに合わせ、同じ単語ばかり選ばれるのを防ぐ
    coerced.setdefault("DUPLICATE", False)

    phrases = [line.xf_kana for line in project.lines]
    result = run_convert(phrases, csv_path, where, coerced)
    apply_converted_lines(project, result["lines"], wordlist, where, coerced)
    return result


def apply_converted_lines(
    project: Project,
    lines: list[dict],
    wordlist: str,
    where: str | None,
    params: dict[str, Any],
) -> None:
    """変換結果の行列([{units, words}])から project.parody を作り直す。

    wordlist はリスト名またはCSVパス(parodyにそのまま保存され、
    import-editor の再取り込みでも同じ解決ができる)。
    """
    csv_path = resolve_wordlist(wordlist)
    rows_by_id = _load_wordlist_rows(csv_path)

    parody = Parody(wordlist=wordlist, where=where, params=params)
    for line, converted in zip(project.lines, lines, strict=True):
        pline = ParodyLine(line_id=line.id)
        unit_lens = [len(u["pronunciation"]) for u in converted["units"]]
        unit_concat = "".join(u["pronunciation"] for u in converted["units"])
        note_lens = [len(project.notes[i].kana) for i in line.note_ids]
        note_concat = "".join(project.notes[i].kana for i in line.note_ids)
        if unit_concat != note_concat:
            logger.debug(
                "行%d: ユニット列と音符列の読みが不一致 (%r != %r)。difflibで対応づけます",
                line.id, unit_concat, note_concat,
            )
        offset_map = _offset_map(unit_concat, note_concat)
        note_cum = [0]
        for length in note_lens:
            note_cum.append(note_cum[-1] + length)

        # 1st pass: 単語ごとに音符割り当てを計算(この時点では複合音符が
        # 単語境界を跨ぐと隣接2単語に二重割り当てされうる)
        notes_kana = [project.notes[i].kana for i in line.note_ids]
        pending: list[list[Any]] = []
        for word in converted["words"]:
            note_idx, note_kana = _map_word_to_notes(
                unit_lens, note_lens, offset_map, tuple(word["period"]),
                word.get("pronunciation"), word.get("kana", ""),
                notes_kana=notes_kana,
            )
            start_c, end_c = _word_char_span(
                unit_lens, offset_map, tuple(word["period"])
            )
            pending.append([word, note_idx, note_kana, start_c, end_c])

        # 2nd pass: 複合音符の二重割り当てを一本化する
        for i, winner, loser in _resolve_shared_notes(pending, note_cum):
            logger.debug(
                "行%d: 音符位置%d を単語 %r と %r が二重取り→%r に一本化",
                line.id, i,
                pending[winner][0]["surface"], pending[loser][0]["surface"],
                pending[winner][0]["surface"],
            )

        # 3rd pass: ParodyWord を生成
        for word, note_idx, note_kana, _start_c, _end_c in pending:
            note_kana = [k or "ー" for k in note_kana]
            if note_idx and all(k == "ー" for k in note_kana):
                logger.warning(
                    "行%d: 単語 %r の歌唱カナがすべて継続(ー)になりました"
                    "(直前の母音を伸ばすだけで単語として聞こえません)",
                    line.id, word["surface"],
                )
            if not note_idx:
                logger.warning(
                    "行%d: 単語 %r を音符に対応づけられずスキップ", line.id, word["surface"]
                )
                continue
            pline.words.append(
                ParodyWord(
                    surface=word["surface"],
                    kana=word["kana"],
                    original=word.get("original", ""),
                    original_surface=word.get("original_surface", ""),
                    originalkana=word.get("originalkana", ""),
                    note_ids=[line.note_ids[i] for i in note_idx],
                    note_kana=note_kana,
                    wordlist_row=_find_row(rows_by_id, word),
                    locked=bool(word.get("locked", False)),
                )
            )
        parody.lines.append(pline)
    project.parody = parody
