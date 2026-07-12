"""同梱editorのconfと単語リストCSVの整合性チェック。

confのfacetsはCSVの列を絞り込みに使うため、片方のsubmoduleだけ更新すると
「列が無くて絞り込みが空振り」「CSVが無くて404」が起きる(実際に危うかった事故)。
submoduleはCIでは取得されない(soramimic本体がprivate)ため、このテストは
ローカルの全テスト実行時=submodule更新をpushする前に効く。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "external" / "soramimic" / "conf" / "setting.json"
WORDLISTS = ROOT / "external" / "soramimic-wordlists"

# facet値の where 述語で使う演算子(長いものを先に並べて分割で誤解釈しない)
_WHERE_OPS = re.compile(r"!~=|~=|!=|=")


def _flatten_wordlist(items: list) -> list[dict]:
    """{label, items:[...]} グループを再帰的に均し、entry列に展開する。"""
    out: list[dict] = []
    for w in items or []:
        if not isinstance(w, dict):
            continue
        if isinstance(w.get("items"), list):
            out.extend(_flatten_wordlist(w["items"]))
        else:
            out.append(w)
    return out


def _conf_entries() -> list[dict]:
    if not CONF.is_file() or not WORDLISTS.is_dir():
        pytest.skip("submodule未取得(CIではsoramimic本体がprivateのため取得しない)")
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    return [
        w
        for w in _flatten_wordlist(conf.get("wordlist", []))
        if w.get("filepath") and w.get("value") != "ORIGINAL" and w.get("active") is not False
    ]


def _where_column(where: str) -> str | None:
    """where述語("col op value")から列名を取り出す。演算子が無ければ None。"""
    m = _WHERE_OPS.search(where or "")
    if not m:
        return None
    return where[: m.start()].strip()


def _facet_columns(facet: dict) -> list[str]:
    """facetが参照する列名の集合。column/columns に加え、各値の where 述語からも
    列名を集める(列指定が無く全値が where を持つfacetにも対応)。"""
    cols = facet.get("columns") or ([facet["column"]] if facet.get("column") else [])
    cols = list(cols)
    for val in facet.get("values") or []:
        col = _where_column(val.get("where", ""))
        if col and col not in cols:
            cols.append(col)
    return cols


def _csv_header(name: str) -> list[str]:
    path = WORDLISTS / f"{name}.csv"
    assert path.is_file(), f"confが参照する単語リストがsubmoduleにありません: {name}.csv"
    with path.open(encoding="utf-8") as f:
        return next(csv.reader(f))


def test_conf_wordlists_exist_and_facet_columns_match():
    entries = _conf_entries()
    assert entries, "confに単語リストがありません"
    for w in entries:
        name = Path(w["filepath"]).stem
        header = [h.strip() for h in _csv_header(name)]
        for facet in w.get("facets") or []:
            # 単数column / 複数columns(例: ポケモンのタイプ=type1,type2)に加え、
            # facet値の where 述語(例: field~=物理)からも列名を集める
            cols = _facet_columns(facet)
            assert cols, f"conf({w.get('text') or name})のfacetに列指定がありません"
            for col in cols:
                assert col in header, (
                    f"conf({w.get('text') or name})のfacet列 {col!r} が "
                    f"{name}.csv のヘッダ {header} にありません"
                )
