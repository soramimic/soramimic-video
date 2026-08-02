"""ファセット絞り込み(where式)の組み立て——editorと同じ形を作るための共有実装。

conf/setting.json の単語リストエントリは、絞り込みをチェックボックス
(facets)で持つ。editor(soramimic frontend/src/convertControls.js)は
チェック状態から where 式を組み立て(compileWhere)、逆に where 式から
チェック状態を復元する(restoreFacets)。

video 側も同じ既定の絞り込みを組み立てる場所が2つある——トップ画面の
``facetDefaultWhere``(static/index.html)と、変換の既定(convert.py)。
3実装が別々の形の式を作っていると、editor に渡した where が復元できず
**絞り込みが黙って消える**(送った条件より広い=安全側でない)ため、
このモジュールが editor 側と1対1に対応する正本を持つ:

===========================  ======================================
convertControls.js            ここ
===========================  ======================================
``facetClause``               :func:`facet_clause`
``compileWhere``              :func:`compile_where` / :func:`default_where`
``containsFragment``          :func:`_contains_fragment`
``restoreFacets``             :func:`restored_where`
===========================  ======================================

形の対応はテスト(tests/test_facets.py)が両方向から固定している:
- static/index.html の facetDefaultWhere が同じ式を作ること(node で実行)
- 全リストで新旧の式が同じ行を選ぶこと(実CSV+エンジンのwhereパーサ)
"""

from __future__ import annotations

from typing import Any


def has_facets(entry: dict[str, Any] | None) -> bool:
    """単語リストエントリがファセット絞り込みを持つか(editorの hasFacets)。"""
    facets = (entry or {}).get("facets")
    return bool(isinstance(facets, list) and facets)


def facet_clause(facet: dict[str, Any], value: dict[str, Any]) -> str:
    """facet の1つの選択肢を where 断片にする(editorの facetClause と同形)。

    - ``value.where`` があればそれをそのまま使う(任意の述語。例: ``field~=物理``)
    - ``facet.columns``(配列)があれば全列の or に展開する
    - どちらも無ければ ``facet.column=値``
    括弧は editor 側と同じく常に付ける——restoreFacets の断片一致が
    区切り(``(`` / `` or `` / `` and `` / ``)``)で挟まれた形を前提にするため。
    """
    if value.get("where"):
        return str(value["where"])
    cols = facet.get("columns") or [facet.get("column")]
    return "(" + " or ".join(f"{c}={value['v']}" for c in cols) + ")"


def _compile_where(clauses: list[list[str]]) -> str:
    """facetごとの断片リストを where 式にする(editorの compileWhere)。

    同一 facet 内は or、facet をまたぐと and。断片が無い facet は制約なし。
    """
    return " and ".join("(" + " or ".join(frags) + ")" for frags in clauses if frags)


def default_where(entry: dict[str, Any] | None) -> str:
    """エントリの既定の絞り込み(facets の ``default: true``)を where 式にする。

    editor が renderFacets(既定チェック)→ compileWhere で作るのと同じ文字列。
    facets を持たないエントリは従来どおり ``entry.where`` をそのまま返す。
    """
    entry = entry or {}
    if not has_facets(entry):
        return str(entry.get("where") or "")
    return _compile_where(
        [
            [facet_clause(f, v) for v in (f.get("values") or []) if v.get("default") is True]
            for f in entry["facets"]
        ]
    )


def _contains_fragment(where: str, frag: str) -> bool:
    """where の中に断片がそのまま(区切りで挟まれた形で)現れるか。

    editorの containsFragment と同じ。``field~=物理`` が ``field~=物理学`` の
    一部にマッチしてしまうのを避ける。
    """
    i = where.find(frag)
    while i >= 0:
        before, after = where[:i], where[i + len(frag) :]
        ok_before = before == "" or before.endswith(("(", " or ", " and "))
        ok_after = after == "" or after.startswith((")", " or ", " and "))
        if ok_before and ok_after:
            return True
        i = where.find(frag, i + 1)
    return False


def restored_where(entry: dict[str, Any] | None, where: str) -> str:
    """where を editor に渡したとき、editor が組み直す where を返す。

    editor は受け取った where からチェック状態を復元し(restoreFacets)、
    変換のときはそのチェック状態から where を組み直す(compileWhere)。
    つまり **editor に実際に効くのはこの戻り値** であって、渡した文字列
    そのものではない。戻り値が入力と一致しないなら、その where は
    チェックボックスで表せない形——渡すと条件が変わる(たいてい広がる)。
    """
    entry = entry or {}
    if not has_facets(entry):
        return where
    return _compile_where(
        [
            [
                facet_clause(f, v)
                for v in (f.get("values") or [])
                if _contains_fragment(where, facet_clause(f, v))
            ]
            for f in entry["facets"]
        ]
    )


def survives_editor_facets(entry: dict[str, Any] | None, where: str | None) -> bool:
    """その where を editor のトップレベルに渡しても条件が変わらないか。"""
    if not has_facets(entry) or not isinstance(where, str):
        return False
    return restored_where(entry, where) == where
