"""替え歌変換ステージ: soramimic ライブラリで行ごとの替え歌単語列を得る。

変換入力はXFの読み(カナ)を行ごとに連結した文字列。変換結果の period は
変換エンジンが返すユニット列(mora単位)へのindexなので、
ユニットの文字オフセット → XFモーラ(音符)の文字オフセット の対応で
各単語を音符ID列に写像する。
"""

from __future__ import annotations

import csv
import difflib
import io
import itertools
import logging
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .kana import (
    normalize_small_vowels,
    open_long_vowel_runs,
    split_fine_moras,
    split_moras,
    vowel_of,
)
from .project import Parody, ParodyLine, ParodyWord, Project
from .soramimic_engine import UnitWeightsFunc, run_convert

logger = logging.getLogger(__name__)


def _engine_kana(kana: str) -> str:
    """エンジンへ渡す読み・突き合わせる音符側の読みに共通で掛ける正規化。

    小書き母音の開き(セェ→セエ)と連続長音の開き(ドーー→ドーオ)。どちらも
    「一致する単語が無い単独ユニットができて行の変換が丸ごと空になる」
    エンジンのトークナイズ起因の問題への前処理。1文字→1文字なので
    文字オフセットの対応は恒等に保たれる。
    """
    return open_long_vowel_runs(normalize_small_vowels(kana))


def engine_phrases(project: Project) -> list[str]:
    """変換エンジンへ渡す行ごとの読み(1行=カナ1文字列)。

    convert_project の入口と、変換せず解析だけする経路(/api/editor-session の
    解析のみモード)で同じ前処理を共有するための小さなヘルパ。
    """
    return [_engine_kana(line.xf_kana) for line in project.lines]


REPO_ROOT = Path(__file__).resolve().parents[2]
WORDLISTS_DIR = REPO_ROOT / "external" / "soramimic-wordlists"


def _is_packaged_wordlist(csv_path: Path) -> bool:
    """同梱の名前付き単語リスト(external/soramimic-wordlists)か。

    既定の絞り込みは conf の列(type・status…)を前提にするので、たまたま同じ
    ファイル名の手元のCSVに当ててはいけない(列が無いと絞り込みが空振りする)。
    """
    try:
        return csv_path.resolve().parent == WORDLISTS_DIR.resolve()
    except OSError:  # 解決できないパスは同梱リストではない
        return False


def default_where(name: str) -> str | None:
    """単語リストの既定の絞り込み(conf/setting.json の facets の default)。

    editor・トップ画面(static/index.html の facetDefaultWhere)と同じ式を
    facets.default_where が組む。以前はここに2リストぶんを直書きしていたが、
    conf に facets を持つリストが増えても更新されず(野球・サッカーのみ・
    サッカーは「役割=選手」が抜けていた)、式の形も editor と違っていたので
    conf から引くようにした。conf が読めない構成では絞り込みなし。
    """
    from .editor_io import conf_wordlist_entry
    from .facets import default_where as facet_default_where

    return facet_default_where(conf_wordlist_entry(name)) or None


def resolve_wordlist(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".csv" and p.exists():
        return p
    candidate = WORDLISTS_DIR / f"{name_or_path}.csv"
    if candidate.exists():
        return candidate
    if re.fullmatch(r"[A-Za-z0-9_-]+", name_or_path):
        from .private_wordlists import resolve as resolve_private_wordlist

        private = resolve_private_wordlist(name_or_path)
        if private is not None:
            return private
    raise FileNotFoundError(
        f"単語リストが見つかりません: {name_or_path} "
        "(同梱/非公開単語リスト名かCSVパスを指定してください)"
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


# soramimic-video 独自の変換パラメータ(本家 soramimic のエンジンには無いキー)。
# エンジンに渡す params dict には載せず、ここで取り出して重み計算に使う。
NOTE_LENGTH_WEIGHT = "NOTE_LENGTH_WEIGHT"


def pop_note_length_weight(params: dict[str, Any]) -> float:
    """params から video 専用パラメータ NOTE_LENGTH_WEIGHT(α)を破壊的に取り出す。

    α はノート長由来のユニット重み ``w_i = (音符の合計秒数) ** α`` の指数。
    0(既定)・負値・数値でない指定はすべて 0.0 = 重み無し(従来動作)を意味する。
    エンジンは未知キーを無害に無視するが、本家に無いキーを混ぜると
    parody.params 経由で editor 側にも漏れるのでここで取り除く。
    """
    raw = params.pop(NOTE_LENGTH_WEIGHT, None)
    if raw is None:
        return 0.0
    try:
        alpha = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s の値が数値ではありません: %r。無効(0)として扱います",
            NOTE_LENGTH_WEIGHT, raw,
        )
        return 0.0
    if not math.isfinite(alpha) or alpha <= 0:
        return 0.0
    return alpha


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


def unit_note_seconds(
    unit_prons: list[str], note_kanas: list[str], note_durs: list[float]
) -> list[float]:
    """各ユニットが覆う音符の合計秒数(raw_i)を求める。

    ユニット(変換エンジンの音節単位)と音符(XFの歌唱モーラ)の対応づけは
    apply_converted_lines と同じ「読みを連結した文字オフセット + _offset_map」で行う。
    ユニットの文字区間に少しでも重なる音符の長さを足す。したがって
    複数音符にまたがるユニット(キャ→キ+ャ)はその合計、複数ユニットを覆う
    長音符(ヨウ)は両ユニットがその長さを受け取る。
    どの音符にも対応づかなかったユニットは行平均の raw で埋める(重みを持たせない)。
    """
    unit_concat = _engine_kana("".join(unit_prons))
    note_concat = _engine_kana("".join(note_kanas))
    offset_map = _offset_map(unit_concat, note_concat)
    note_cum = [0]
    for kana in note_kanas:
        note_cum.append(note_cum[-1] + len(kana))

    raws: list[float | None] = []
    pos = 0
    for pron in unit_prons:
        start, end = pos, pos + len(pron)
        pos = end
        # ユニットの文字区間を音符側のオフセットへ写す
        dst_start = offset_map[min(start, len(unit_concat))]
        dst_end = offset_map[min(end, len(unit_concat))]
        total = 0.0
        hit = False
        for j in range(len(note_kanas)):
            a, b = note_cum[j], note_cum[j + 1]
            if a < dst_end and dst_start < b:  # 文字区間が重なる音符
                total += note_durs[j]
                hit = True
        raws.append(total if hit else None)

    known = [v for v in raws if v is not None]
    mean = sum(known) / len(known) if known else 1.0
    return [mean if v is None else v for v in raws]


def note_length_weights(
    unit_prons: list[str], note_kanas: list[str], note_durs: list[float], alpha: float
) -> list[float]:
    """ノート長由来のユニット重み ``w_i = raw_i ** α``(1行分)。

    正規化(行ごとに平均1)は変換エンジン側が行うのでここではしない。
    """
    return [
        (raw**alpha if raw > 0 else 0.0)
        for raw in unit_note_seconds(unit_prons, note_kanas, note_durs)
    ]


def project_note_length_weights(project: Project, alpha: float) -> UnitWeightsFunc:
    """run_convert に渡す「ユニット列 → 行ごとのノート長重み」コールバックを作る。

    重み計算にはエンジンが実際に使うユニット列が要るので、run_convert から
    呼び戻してもらう形にしている(トークナイズを二重に走らせないため)。
    """

    def compute(units_per_line: list[list[dict[str, Any]]]) -> list[list[float]]:
        out: list[list[float]] = []
        for line, units in zip(project.lines, units_per_line, strict=True):
            notes = [project.notes[i] for i in line.note_ids]
            out.append(
                note_length_weights(
                    [u["pronunciation"] for u in units],
                    [n.kana for n in notes],
                    [n.end_sec - n.start_sec for n in notes],
                    alpha,
                )
            )
        return out

    return compute


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
    True にする。子音付きのi/u段モーラ(ディ・キ・ク・ル等)は対象外。
    空文字(ルビ無し漢字ノートなど kana が空)は False 扱い。"""
    char_to_vowel, _ = _kana_phon()
    flags: list[bool] = []
    for i, m in enumerate(moras):
        drop = m in _DROPOUT_MORA
        if not drop and i > 0 and m in _CHAIN_SECOND_VOWEL:
            prev = _rep_mora(moras[i - 1])
            drop = bool(prev) and char_to_vowel(prev) == _CHAIN_SECOND_VOWEL[m]
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
    if not er or not nr:  # 空kana(ルビ無し漢字ノート等)は照合対象外
        return 0
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
# (ッ/ン/ー/エイ・オウ連鎖のイ・ウ)」ほど載せやすいボーナスで評価する
# (複合ノート ダッ/テイ への積みボーナスは、積む要素が脱落系のときだけ)。

# 脱落系モーラを同一音符へ積む優先ボーナス。母音一致(1000)より小さくし、
# 母音一致数を犠牲にしてまで脱落系ペアを作らない。
_STACK_BONUS = 200
# 元音符が複数モーラ(閉音節・長音)のとき、その音符へ積む追加ボーナス。
# 複合ノート(タイ・ダッ等)は実アタック1つなので、積む要素が脱落系(ッ/ン/ー/
# エイ・オウ連鎖のイ・ウ)のときだけ与える。実音節の積みは優遇しない
# (溢れ時に積むこと自体は依然可能)
_STACK_NOTE_BONUS = 100
# 音符のアタック数(容量)に対する超過1音節あたりのペナルティ。
# 母音一致(1000)より重くし、詰め込みは母音が合っても避ける
_CAP_OVER_PENALTY = 1200
# 載せる音節数が音符のアタック数と一致したときのボーナス(配分の鏡写しを優遇)。
# 脱落系スタックボーナス(200)より小さくし、マン・クン等の閉音節ペアを
# 容量一致のために引き剥がさない
_CAP_MATCH_BONUS = 50
# 引き伸ばしノート(ハァ/セェ/オー等: 2文字目以降が小書き母音・ー)に
# 2音節以上積むときの追加ペナルティ(1音節を保持したまま歌う音符のため)。
# 音符が行内中央値より十分長い場合は免除する(長い音符には積んでよい)
_HELD_STACK_PENALTY = 200
# 二重母音の結合ボーナス: 母音単独のイ/ウが直前モーラに続く(アイ/オイ/ソウ等)
# とき、同じ音符に載せる(音節を分断しない)ことを優遇する
_DIPHTHONG_BONUS = 250

# 文節頭ノートへの詰め込み(2音節以上)1音節あたりのペナルティ。
# 文節の立ち上がりは息継ぎ・アタックの位置なので実音1つで刻む方が歌いやすい
_BUNSETSU_HEAD_STACK = 150
# 文節頭ノートを継続ー(空ユニット)にするペナルティ(語頭固定の文節版)
_BUNSETSU_HEAD_BAR = 150

# 引き伸ばしノート判定に使う継続文字(小書き母音・長音)
_HELD_CHARS = set("ァィゥェォー")

_phrase_splitter: Any = None
_phrase_splitter_failed = False


def _bunsetsu_head_flags(surfaces: list[str]) -> list[bool] | None:
    """行内の各音符が文節頭かどうか(漢字かな混じりsurfaceから)。

    XFのsurfaceは文節頭側の音符に文字が乗り、継続音符は空になる。音符surface
    の連結を jphrase で文節分割し、文節開始文字を含む音符を True にする。
    jphrase が無い・surfaceが空・分割結果が突合しないときは None(機能オフ=
    後方互換。読み上げカナしか無い入力でも従来どおり動く)。
    """
    global _phrase_splitter, _phrase_splitter_failed
    if _phrase_splitter_failed:
        return None
    text = "".join(surfaces)
    if not text:
        return None
    if _phrase_splitter is None:
        try:
            from jphrase import PhraseSplitter

            _phrase_splitter = PhraseSplitter()
        except Exception:  # ImportError・辞書無しなど。以後は試さない
            _phrase_splitter_failed = True
            return None
    try:
        phrases = _phrase_splitter.split_text(text)
    except Exception:
        return None
    if "".join(phrases) != text:
        return None
    starts = set()
    off = 0
    for ph in phrases:
        starts.add(off)
        off += len(ph)
    flags = []
    off = 0
    for s in surfaces:
        flags.append(bool(s) and off in starts)
        off += len(s)
    return flags


def _stack_bonus(elem_drop: bool, note_multi: bool) -> int:
    """区間2要素目以降(=同じ音符に積む要素)のボーナス。

    脱落系モーラ(ッ/ン/ー/エイ・オウ連鎖のイ・ウ)は音符へ積んでも実アタックが
    増えないので優遇する。複合ノート(ダッ/テイ等)への追加ボーナスも脱落系限定:
    複合ノートも実アタックは1つで、実音節を積めば原曲に無い発音が音符の途中に
    立つため、積みを推奨しない(溢れ時に積むこと自体は依然可能)。
    """
    if not elem_drop:
        return 0
    return _STACK_BONUS + (_STACK_NOTE_BONUS if note_multi else 0)


def _note_attacks(kana: str) -> int:
    """音符カナの実アタック数(=載せられる実音節数の目安)。

    複合ノート(タイ・コイ・マン・ダッ等)は元歌でも1回の発音で歌われ、2モーラ目は
    短い渡り・閉じ音でしかない。モーラ数をそのまま容量にすると「2音節入る容器」と
    誤認して積みを誘発し、合成側でノート中央に2音節目が立って遅れて聞こえる。
    2モーラ目以降が添えかな(二重母音のイ/ウ・撥音ン・促音ッ)なら数えない。
    小書きカナ・長音ーは split_moras が直前へ結合するのでここには現れない。
    """
    moras = split_moras(kana)
    cnt = 0
    for i, m in enumerate(moras):
        if i > 0 and _tail_mora(m, moras[i - 1]):
            continue
        cnt += 1
    return max(1, cnt)


def _tail_mora(mora: str, prev: str) -> bool:
    """直前モーラと同じアタックで歌われる添えかなか(ン・ッ・二重母音のイ/ウ)。"""
    if mora in ("ン", _SOKUON):
        return True
    if mora in ("イ", "ウ"):  # 母音単独のイ/ウのみ(キ・ル等は独立アタック)
        return vowel_of(prev) is not None
    return False


def _held_note(kana: str) -> bool:
    """ハァ・セェ・オー のような「1音節を引き伸ばした」音符かどうか。
    2文字目以降がすべて小書き母音か長音なら True(ダッ/ナン/テイは False)。"""
    return len(kana) >= 2 and all(c in _HELD_CHARS for c in kana[1:])


def _overflow_alloc(
    pronunciation: list[str],
    heads: list[str],
    elem_drop: list[bool],
    note_drop: list[bool],
    id_kanas: list[str],
    note_durs: list[float] | None = None,
    note_bheads: list[bool] | None = None,
) -> list[str] | None:
    """溢れ時の音符ごとの歌唱カナ。要素列を音符数個の連続非空区間へ分割する。

    dp[j][t] = 先頭 j 要素を先頭 t 音符に割り当てた最良スコア。
    区間が促音ッで始まる分割は閉音節ペアの分断なので不可。区間の評価は
    先頭要素の _pair_score(母音一致優先)に加え、音符のアタック数(容量)への
    一致・超過、引き伸ばしノートへの積み込み、二重母音の結合を加点減点する。
    note_durs(音符の長さ秒。省略時は長さ由来の項が無効=後方互換)があれば、
    行内中央値より短い音符への容量超過を追加減点し、十分長い音符では
    引き伸ばしペナルティを免除する。同点は従来動作(末尾寄せ)に寄せる。
    実行可能な分割がなければ None(従来の左詰めへフォールバック)。
    """
    n, k = len(pronunciation), len(id_kanas)
    if n <= k or k == 0:
        return None

    # 容量はモーラ数ではなくアタック数(タイ・マン等の複合ノートは1)
    note_caps = [_note_attacks(kana) for kana in id_kanas]
    note_held = [_held_note(kana) for kana in id_kanas]
    # 添えかな付きの複合ノート(タイ・ウッ等)。引き伸ばしノートと同じく、替え歌側の
    # 二重母音(アイ・オイ)は添えかなの位置に収まるので1音節として数える
    note_compound = [
        len(split_moras(kana)) > cap
        for kana, cap in zip(id_kanas, note_caps, strict=True)
    ]
    if note_durs is not None and len(note_durs) == k:
        med = sorted(note_durs)[k // 2] or 0.0
        note_short = [med > 0 and dur < 0.6 * med for dur in note_durs]
        note_long = [med > 0 and dur >= 1.2 * med for dur in note_durs]
    else:
        note_short = [False] * k
        note_long = [False] * k

    # 二重母音ペア: 要素jが母音単独のイ/ウで直前要素に続く(アイ・オイ・ソウ等)。
    # 直後がンのときは イン/ウン の閉音節を優先し、結合しない
    diph = [False] * n
    for j in range(1, n):
        if pronunciation[j] not in ("イ", "ウ"):
            continue
        if not heads[j - 1] or heads[j - 1] == "ー":
            continue
        if j + 1 < n and pronunciation[j + 1] == "ン":
            continue
        diph[j] = True

    def eff_moras(a: int, b: int, allow_diph: bool) -> int:
        """区間 [a,b) の実効音節数。「ー」と区間末尾の「ン」は引き伸ばし・
        ハミングとしてどの音符でも自然に歌えるため容量に数えない
        (サー=1、マン=1、ゼニ=2)。allow_diph のとき(引き伸ばし・複合ノート)は
        二重母音のイ/ウ(オイ・コウ等)も直前と同音節として数えない。"""
        cnt = 0
        for j in range(a, b):
            if allow_diph and j > a and diph[j]:
                continue  # 二重母音の2モーラ目は直前と同音節
            cnt += sum(1 for m in split_moras(pronunciation[j]) if m != "ー")
        last = pronunciation[b - 1].rstrip("ー")
        if cnt > 1 and last.endswith("ン"):
            cnt -= 1
        return max(1, cnt)

    def seg_score(a: int, b: int, t: int) -> int | None:
        """要素 [a,b) を音符 t に載せるスコア。ッ頭は不可。"""
        if heads[a].startswith(_SOKUON):
            return None
        s = _pair_score(heads[a], id_kanas[t], elem_drop[a], note_drop[t])
        note_multi = len(split_moras(id_kanas[t])) >= 2
        for j in range(a + 1, b):
            s += _stack_bonus(elem_drop[j], note_multi)
            if diph[j]:  # 二重母音を同じ音符で保つ
                s += _DIPHTHONG_BONUS
        moras = eff_moras(a, b, note_held[t] or note_compound[t])
        over = max(0, moras - note_caps[t])
        s -= _CAP_OVER_PENALTY * over
        if moras == note_caps[t]:
            s += _CAP_MATCH_BONUS
        if note_held[t] and not note_long[t] and moras > 1:
            s -= _HELD_STACK_PENALTY * (moras - 1)
        if note_short[t] and over:
            s -= _CAP_OVER_PENALTY * over  # 短い音符への超過は倍のペナルティ
        if note_bheads is not None and note_bheads[t] and moras > 1:
            s -= _BUNSETSU_HEAD_STACK * (moras - 1)  # 文節頭は実音1つで刻む
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
    bheads: list[bool] | None = None,
) -> int | None:
    """音節 u に要素 [start, start+length) を載せたときの内側配置スコア合計。
    促音ッのペアが分断(区間先頭がッ)・隣接不能な区間は制約違反として None を返す。"""
    if length <= 0:
        # 空ユニット(継続ー)。文節頭の音符をーにするのは避ける(語頭固定の文節版)
        if bheads is not None and any(bheads[k] for k in unit_note_ks[u]):
            return -_BUNSETSU_HEAD_BAR
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
    bheads: list[bool] | None = None,
) -> int | None:
    """個数配分 e の内側配置スコア合計。制約違反を含むなら None。"""
    s = 0
    start = 0
    for u, cnt in enumerate(e):
        seg = _seg_score(
            heads, id_kanas, elem_drop, note_drop, unit_note_ks, u, start, cnt,
            paired, bheads,
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
    bheads: list[bool] | None = None,
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
                    u - 1, j, i - j, paired, bheads,
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


# 圧縮長音の展開候補を数える上限(1単語あたり)。単語は数要素しかないので
# 実際にはまず届かないが、展開スロットが多い長い単語でDPが増えないよう蓋をする
_EXPAND_CANDIDATE_LIMIT = 64


def _expansion_candidates(
    pronunciation: list[str],
    comp_per_elem: list[list[str]] | None,
    budget: int,
    limit: int = _EXPAND_CANDIDATE_LIMIT,
) -> list[tuple[list[str], list[list[str]]]]:
    """圧縮で「ー」に潰れた単語モーラを独立要素へ戻した発音要素列の候補を返す。

    変換エンジンは単語の1音節を ``[頭] + ー``(撥音・長音・母音字を長音へ圧縮)に
    することがあり(セイショウナゴン → セー/ショー/ナ/ゴ/ン)、そのぶん要素数が
    音符数より少なくなって余り音符が継続「ー」になる。ここでは
    _compressed_moras_per_element が求めた圧縮モーラを ``[頭] + モーラ`` の
    2要素へ戻す組み合わせを列挙し、余り音符へ実音を載せられるようにする。

    - 展開対象は圧縮由来の「ー」だけ。単語自身の長音(genuine)は頭側に残すので、
      単語kanaに無いモーラを捏造しない
    - budget(=音符数-要素数)を超える候補は作らない(溢れさせない)
    - 返る順は「追加要素が多い候補が先、同数なら前方の要素を展開する候補が先」

    各候補は (発音要素列, 要素ごとの残り圧縮モーラ) の対で、展開した要素の
    圧縮モーラは空になる(同じモーラを二重に復元しない)。
    """
    if not comp_per_elem or budget <= 0:
        return []
    slots = [j for j, comp in enumerate(comp_per_elem) if comp]
    if not slots:
        return []
    out: list[tuple[list[str], list[list[str]]]] = []
    for r in range(len(slots), 0, -1):
        for combo in itertools.combinations(slots, r):
            if sum(len(comp_per_elem[j]) for j in combo) > budget:
                continue
            prons: list[str] = []
            comps: list[list[str]] = []
            for j, p in enumerate(pronunciation):
                if j not in combo:
                    prons.append(p)
                    comps.append(comp_per_elem[j])
                    continue
                comp = comp_per_elem[j]
                head = p.rstrip("ー")
                genuine = (len(p) - len(head)) - len(comp)  # 単語自身の長音ぶんのー
                prons.append(head + "ー" * genuine)
                comps.append([])
                for mora in comp:
                    prons.append(mora)
                    comps.append([])
            out.append((prons, comps))
            if len(out) >= limit:
                return out
    return out


def _map_word_to_notes(
    unit_lens: list[int],
    note_lens: list[int],
    offset_map: list[int],
    period: tuple[int, int],
    pronunciation: list[str] | None = None,
    word_kana: str = "",
    notes_kana: list[str] | None = None,
    notes_dur: list[float] | None = None,
    notes_bunsetsu: list[bool] | None = None,
) -> tuple[list[int], list[str]]:
    """periodユニット区間 → 重なる音符indexの列と音符ごとの歌唱カナ。

    発音要素(pronunciation)は変換エンジンが period 内の元歌詞ユニット
    (units=音節)のバリエーション(syllableToVariation)と要素数を揃えて
    マッチさせた結果なので、要素は「元歌詞ユニット」を単位に音符へ載る。
    ユニット境界を尊重して要素を音符へ配置し、さらに単語側の圧縮モーラ
    (撥音等)を同ユニット内の空き音符へ復元する。

    notes_kana(行の音符ごとの元kana、note_lens と並行)を渡すと、ユニット内で
    要素をどの音符に載せるか(=どの音符を継続ーにするか)を母音一致優先のDPで
    決める。ユニット境界はハード制約で、音符集合(ids)は変わらない。さらに、
    それでも余り音符(継続ー)が残るときは、圧縮で「ー」に潰れた単語モーラを
    独立要素へ戻す候補(_expansion_candidates)を試し、埋まる音符が増えるものを
    採用する(同数なら割り付けスコアが良い方)。
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
    id_durs = [notes_dur[i] for i in ids] if notes_dur is not None else None
    id_bheads = [notes_bunsetsu[i] for i in ids] if notes_bunsetsu is not None else None

    # 溢れ(要素数>音符数)は、要素列を音符数個の連続区間に分割するDPで
    # 音符ごとの歌唱カナを直接決める(従来は末尾音符へ丸ごと寄せていた)。
    # 全音符が実音を持つのでユニット内の空き音符への圧縮復元は不要。
    if id_kanas is not None and len(pronunciation) > len(ids):
        assert elem_drop is not None and note_drop is not None
        alloc = _overflow_alloc(
            pronunciation, heads, elem_drop, note_drop, id_kanas, id_durs, id_bheads
        )
        if alloc is not None:
            return ids, alloc

    n_units = len(units)

    def alloc_with(
        prons: list[str], comps: list[list[str]] | None
    ) -> tuple[list[str], int | None]:
        """発音要素列 prons を音符へ載せた (音符ごとの歌唱カナ, 割り付けスコア)。

        スコアは採用した個数配分の内側配置スコア合計(母音一致優先)で、
        同じ音符集合に対する別の要素列(圧縮長音の展開候補)同士を比べるのに使う。
        制約を満たす配分が定義できない(notes_kana 無し・溢れ)ときは None。
        """
        kana_out = [""] * len(ids)
        hd = [p.rstrip("ー") for p in prons]
        e_drop = _dropout_flags(hd) if id_kanas is not None else None
        # 替え歌側の促音ッ+直前モーラの閉音節ペア(不可分・隣接音符ハード制約)
        pr = _paired_sokuon(hd) if id_kanas is not None else None

        # 要素→音節(ユニット)の個数配分。既定は従来の位置ベース。notes_kana があり
        # 溢れ(要素数>音符数)でなければ、音節内配置スコア総和を最大化する外側DPで
        # 最適化する(音節対応は維持、促音ッの閉音節ペアは分断しない、同点は現行配分)。
        e = _positional_distribution(len(prons), unit_note_ks)
        score: int | None = None
        if id_kanas is not None and n_units and len(prons) <= len(ids):
            e_opt, opt_score = _distribute_moras(
                hd, id_kanas, e_drop, note_drop,  # type: ignore[arg-type]
                unit_note_ks, len(prons), pr, id_bheads,
            )
            pos_score = _distribution_score(
                e, hd, id_kanas, e_drop, note_drop, unit_note_ks, pr, id_bheads
            )
            # e_opt が制約を満たし、かつ現行配分より良い(または現行が制約違反)なら採用
            if e_opt is not None and (pos_score is None or opt_score > pos_score):
                e = e_opt
                score = opt_score
            else:
                score = pos_score

        base = 0
        for ui in range(n_units):
            eu = e[ui]
            ks = unit_note_ks[ui]
            # 音節内(内側)で eu 要素を ks 音符へ載せる位置を決める(母音一致優先DPまたは
            # 左詰め)。ユニット境界(ks)はハード制約。語頭要素は先頭音符に固定。促音ッの
            # 閉音節ペアは直前モーラと隣接音符へ固定する。
            adj_local = [pr[base + j] for j in range(eu)] if pr is not None else None
            pos = _inner_positions(
                hd, id_kanas, e_drop, note_drop, ks, base, eu,
                force_first=(base == 0 and eu > 0), adj=adj_local,
            )
            if pos is None:  # 制約充足不能(フォールバック): 従来の左詰め
                pos = [min(j, len(ks) - 1) for j in range(eu)] if ks else []
            for j in range(eu):
                p = prons[base + j]
                if not ks:
                    continue
                k = ks[pos[j]]
                is_last = j == eu - 1
                comp = comps[base + j] if (is_last and comps) else []
                if comp:
                    trailing = [
                        kk
                        for kk in ks[pos[j] + 1 :]
                        if kana_out[kk] == "" and owners[kk] == 1
                    ]
                    r = min(len(comp), len(trailing))
                    head = p.rstrip("ー")
                    genuine = (len(p) - len(head)) - len(comp)  # 単語自身の長音ぶんのー
                    kana_out[k] += head + "ー" * (genuine + (len(comp) - r))
                    for x in range(r):
                        kana_out[trailing[x]] = comp[x]
                else:
                    kana_out[k] += p
            base += eu
        return kana_out, score

    kana_per_note, _score = alloc_with(pronunciation, comp_per_elem)

    # 余り音符(実音が載らず継続ーになる音符)が残るなら、圧縮で「ー」に潰れた
    # 単語モーラを元に戻して埋め直せないか試す(_expansion_candidates)。
    # ユニット内の空き音符への復元だけでは、圧縮された要素と余り音符が別ユニットに
    # なるケース(セイショウナゴン: セー ショー ナ ゴ ン + 余り1音符)を埋められず、
    # 残った「ー」が撥音ンの後に来るとNEUTRINO側のガードで「ア」に化けてしまう。
    if id_kanas is not None and "" in kana_per_note:
        best_fill = kana_per_note.count("")
        best_score: int | None = None  # ベースラインはスコア比較の対象外
        for prons, comps in _expansion_candidates(
            pronunciation, comp_per_elem, len(ids) - len(pronunciation)
        ):
            cand_kana, cand_score = alloc_with(prons, comps)
            if cand_score is None:  # 制約を満たす配分が無い候補は捨てる
                continue
            cand_fill = cand_kana.count("")
            if cand_fill > best_fill:  # 埋まる音符が減るなら意味が無い
                continue
            # 埋まる音符数を最優先、同数なら割り付けスコア(母音一致優先)で選ぶ
            if cand_fill == best_fill and (
                best_score is None or cand_score <= best_score
            ):
                continue
            kana_per_note, best_fill, best_score = cand_kana, cand_fill, cand_score

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
    with open(csv_path, encoding="utf-8") as f:
        return _rows_by_id(f)


def wordlist_rows_from_text(text: str) -> dict[str, list[dict[str, str]]]:
    """CSVテキストから id → 行 の索引を作る(ファイルに置かない単語リスト用)。

    editor.json に同梱されてくる自作リスト(csvText)のように、サーバー上の
    ファイルとして存在しないリストを _find_row に渡すための入口。
    """
    return _rows_by_id(io.StringIO(text))


def _rows_by_id(lines: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(lines):
        rows.setdefault(row.get("id", ""), []).append(row)
    return rows


def _find_row(
    rows_by_id: dict[str, list[dict[str, str]]], word: dict
) -> dict[str, str] | None:
    # filler(万能候補)は単語リストの語ではない仮想語で id を持たない。
    # id 無しのまま引くと、id列が空のCSV行(rows_by_id の "" バケツ)に
    # 誤って当たって別の単語の画像が出てしまうので、先に弾く。
    if word.get("filler") or "id" not in word:
        return None
    rows = rows_by_id.get(str(word.get("id", "")))
    if not rows:
        return None
    for row in rows:
        if row.get("surface") == word.get("surface"):
            return row
    return rows[0]


def resolve_convert_settings(
    csv_path: Path | None,
    where: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any], float]:
    """変換の実効設定 (where, エンジンに渡すparams, NOTE_LENGTH_WEIGHTのα) を決める。

    convert_project の入口と、曲名だけを同じ条件で変換したいところ
    (thumbnail.py のサムネ生成・そのプレビュー)で共有する。
    渡された params は壊さない(コピーして返す)。

    csv_path は単語リストごとの既定の絞り込み(:func:`default_where`)を引く
    ためだけに使う。単語リストが決まっていない場面(変換せず解析だけする
    /api/editor-session)では None を渡してよい——その場合 where は渡された
    ものをそのまま使う。
    """
    if where is None and csv_path is not None and _is_packaged_wordlist(csv_path):
        where = default_where(csv_path.stem)
    # video 専用パラメータはエンジンへ渡す前に取り除く(呼び出し側のdictは壊さない)
    params = dict(params or {})
    alpha = pop_note_length_weight(params)
    coerced = _coerce_params(params)
    # エンジン既定はDUPLICATE:true(単語重複あり)だが、本家Web UIの既定は
    # 「なし」。未指定時はWeb UIに合わせ、同じ単語ばかり選ばれるのを防ぐ
    coerced.setdefault("DUPLICATE", False)
    # 未指定パラメータは本家Web UIの既定プリセット「バランス」
    # (r=0.8・文節1・単語長2 → getParam の写像値)に合わせる。CLI・APIの
    # パラメータ無し変換も同梱Web UI・本家soramimic.comと同じ出力になる
    coerced.setdefault("VOWEL_RATIO", 0.8)
    coerced.setdefault("SAME_PHRASE_BREAK_REWARD", 0)
    coerced.setdefault("MID_PHRASE_BREAK_PENALTY", 20)
    coerced.setdefault("WORD_NUMBER_PENALTY", 20)
    # ン/ッ/ー変換コストは本家と同じく r に連動(実効=20×r。editor.jsの補完と同じ)
    try:
        r = float(coerced["VOWEL_RATIO"])
    except (TypeError, ValueError):  # 数値でない指定はエンジン側の既定と同じ0.8扱い
        r = 0.8
    coerced.setdefault("VARIATION_COST", 20 * r)
    return where, coerced, alpha


def convert_project(
    project: Project,
    wordlist: str,
    where: str | None = None,
    params: dict[str, str] | None = None,
    cache_db: bool = True,
) -> dict:
    """project.parody を埋める(破壊的)。変換エンジンの生の応答を返す。

    生の応答(units・period付きの単語列・tokensList)は editor 連携の
    書き出しに必要なので、呼び出し側でプロジェクトディレクトリに保存する。

    cache_db=False は単語DBの共有キャッシュを使わない指定。アップロードされた
    自作リスト(そのジョブ限りのCSV)のように使い回しの効かない入力で使う。
    """
    csv_path = resolve_wordlist(wordlist)
    where, coerced, alpha = resolve_convert_settings(csv_path, where, params)

    # 同母音の小書き(ウッセェワ)はエンジンのトークナイズで「セ」「ェ」に割れ、
    # 「ェ」に一致する単語が無いためその行の変換結果が空になる。1文字→1文字で
    # 開いて(ウッセエワ)から渡す(文字数もモーラ位置も変わらない)
    phrases = engine_phrases(project)

    # α>0 のときだけ重みを渡す。α=0(既定)は weights_per_line=None で従来と完全に同一
    weights = project_note_length_weights(project, alpha) if alpha > 0 else None
    result = run_convert(
        phrases, csv_path, where, coerced, weights_per_line=weights, cache_db=cache_db
    )
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
        # 小書き母音の開き(セェ→セエ)はエンジンに渡す側だけに掛かるので、
        # 突き合わせる音符側にも同じ正規化を掛けて位置対応を恒等に保つ
        unit_concat = _engine_kana(
            "".join(u["pronunciation"] for u in converted["units"])
        )
        note_lens = [len(project.notes[i].kana) for i in line.note_ids]
        note_concat = _engine_kana(
            "".join(project.notes[i].kana for i in line.note_ids)
        )
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
        notes_dur = [
            project.notes[i].end_sec - project.notes[i].start_sec
            for i in line.note_ids
        ]
        notes_bunsetsu = _bunsetsu_head_flags(
            [project.notes[i].surface for i in line.note_ids]
        )
        pending: list[list[Any]] = []
        for word in converted["words"]:
            note_idx, note_kana = _map_word_to_notes(
                unit_lens, note_lens, offset_map, tuple(word["period"]),
                word.get("pronunciation"), word.get("kana", ""),
                notes_kana=notes_kana, notes_dur=notes_dur,
                notes_bunsetsu=notes_bunsetsu,
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
                    filler=bool(word.get("filler", False)),
                )
            )
        parody.lines.append(pline)
    project.parody = parody
