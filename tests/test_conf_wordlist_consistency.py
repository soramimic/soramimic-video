"""同梱editorのconfと単語リストCSVの整合性チェック。

confのfacetsはCSVの列を絞り込みに使うため、片方のsubmoduleだけ更新すると
「列が無くて絞り込みが空振り」「CSVが無くて404」が起きる(実際に危うかった事故)。
submoduleはCIでは取得されない(soramimic本体がprivate)ため、このテストは
ローカルの全テスト実行時=submodule更新をpushする前に効く。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "external" / "soramimic" / "conf" / "setting.json"
WORDLISTS = ROOT / "external" / "soramimic-wordlists"


def _conf_entries() -> list[dict]:
    if not CONF.is_file() or not WORDLISTS.is_dir():
        pytest.skip("submodule未取得(CIではsoramimic本体がprivateのため取得しない)")
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    return [
        w
        for w in conf.get("wordlist", [])
        if w.get("filepath") and w.get("value") != "ORIGINAL" and w.get("active") is not False
    ]


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
            # 単数column / 複数columns(例: ポケモンのタイプ=type1,type2)の両形式
            cols = facet.get("columns") or (
                [facet["column"]] if facet.get("column") else []
            )
            assert cols, f"conf({w.get('text') or name})のfacetに列指定がありません"
            for col in cols:
                assert col in header, (
                    f"conf({w.get('text') or name})のfacet列 {col!r} が "
                    f"{name}.csv のヘッダ {header} にありません"
                )
