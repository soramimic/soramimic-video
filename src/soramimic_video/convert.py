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


def _dropout_flags(moras: list[str]) -> list[bool]:
    """各モーラが脱落・長音化しやすいか。特殊モーラ(ッ/ン/ー)と、直前が e段+イ・
    o段+ウ となるエイ/オウ型連鎖の2モーラ目を True にする。"""
    char_to_vowel, _ = _kana_phon()
    flags: list[bool] = []
    for i, m in enumerate(moras):
        drop = m in _DROPOUT_MORA
        if not drop and i > 0:
            pv = char_to_vowel(_rep_mora(moras[i - 1]))
            if (m == "イ" and pv == "エ") or (m == "ウ" and pv == "オ"):
                drop = True
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
    scores: list[list[int]], eu: int, k: int, force_first: bool
) -> list[int]:
    """eu 個の要素を k 個の音符(0..k-1)へ、順序を保った単調増加の位置列で割り当て、
    スコア総和を最大化する(DP)。同点は前方の位置を優先。force_first のとき先頭要素は
    音符0に固定(語頭の継続ー化を避ける)。余った音符は空(=継続ー)になる。eu<=k 前提。
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
        for kk in range(j, k):  # 要素jは最短でも位置j
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
    pos = [0] * eu
    kk = best_k
    for j in range(eu - 1, -1, -1):
        pos[j] = kk
        kk = par[j][kk]
    return pos


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

    # 発音要素(計 len(pronunciation))をユニットへ分配する。
    # 各ユニットは最低1要素、余剰は音符の空きがあるユニットへ左から割り当てる
    # (エンジンは長い音節を複数要素へ展開しうる。例: ユウ→[ユ,ウ])。
    n_units = len(units)
    e = [0] * n_units
    remaining = len(pronunciation)
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
        id_kanas = [notes_kana[i] for i in ids]
        elem_drop = _dropout_flags(heads)
        note_drop = _dropout_flags(id_kanas)
    else:
        id_kanas = None

    base = 0
    for ui in range(n_units):
        eu = e[ui]
        ks = unit_note_ks[ui]
        # ユニット内で eu 要素を ks 音符へ載せる位置 pos(ks内index)を決める。
        # notes_kana があり音符数>要素数のときだけ母音一致優先DPで選び、それ以外は
        # 従来の左詰め(位置ベース)。ユニット境界(ks)は常にハード制約。
        if id_kanas is not None and 0 < eu < len(ks):
            svec = [
                [
                    _pair_score(
                        heads[base + j], id_kanas[ks[kk]],
                        elem_drop[base + j], note_drop[ks[kk]],
                    )
                    for kk in range(len(ks))
                ]
                for j in range(eu)
            ]
            pos = _align_positions(svec, eu, len(ks), force_first=(ui == 0))
        else:
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
