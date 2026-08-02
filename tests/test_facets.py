"""ファセット絞り込み(where式)の形が3実装で一致していること。

where式を組む場所は3つある:

1. editor(external/soramimic frontend/src/convertControls.js の
   renderFacets + compileWhere)——チェック状態から組み、restoreFacets で
   逆に読み戻す
2. トップ画面(src/soramimic_video/static/index.html の facetDefaultWhere)
3. サーバー(src/soramimic_video/facets.py。CLI・APIの既定の絞り込み)

形がずれると、video が渡した where を editor が復元できず、チェックが全部
外れた状態で組み直されて**絞り込みが丸ごと消える**(送った条件より広くなる=
安全側でない)。実際にそれで PR #200 では where をシードのトップレベルに
渡せずにいた。ここでは 1 と 2 を node で実際に走らせて 3 と突き合わせ、
さらに新旧の式が同じ行を選ぶことを実CSVで確かめる。

submodule(conf・単語リストCSV)とnodeが要るので、無い環境ではskipする
(CIではsoramimic本体がprivateで取得しないため、ローカルの全テスト実行=
submodule更新をpushする前に効く)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from soramimic_video.facets import default_where, restored_where, survives_editor_facets

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "external" / "soramimic" / "conf" / "setting.json"
CONVERT_CONTROLS = ROOT / "external" / "soramimic" / "frontend" / "src" / "convertControls.js"
WORDLISTS = ROOT / "external" / "soramimic-wordlists"
INDEX_HTML = ROOT / "src" / "soramimic_video" / "static" / "index.html"

# editor の renderFacets が要求する最小限のDOM。実装が別のDOM APIを使い始めたら
# ここで落ちる(=形の一致を確かめられなくなったことに気づける)。
DOM_SHIM = """
class El {
	constructor(tag) {
		this.tag = tag; this.children = []; this.className = ""; this.textContent = "";
	}
	set innerHTML(v) {
		if (v !== "") throw new Error("innerHTMLへの代入はクリアだけを想定: " + v);
		this.children = [];
	}
	appendChild(c) { this.children.push(c); return c; }
	append(...cs) { this.children.push(...cs); }
	querySelectorAll(sel) {
		const hit = (e) => sel === ".facet-group" ? e.className === "facet-group"
			: sel === "input:checked" ? e.tag === "input" && e.checked
			: sel === "input[type=checkbox]" ? e.tag === "input" && e.type === "checkbox"
			: (() => { throw new Error("未対応のセレクタ: " + sel); })();
		const out = [];
		const walk = (e) => {
			for (const c of e.children || []) { if (hit(c)) out.push(c); walk(c); }
		};
		walk(this);
		return out;
	}
}
globalThis.document = {
	createElement: (tag) => new El(tag),
	createTextNode: (t) => ({ tag: "#text", textContent: t, children: [] }),
};
"""


def _conf_entries() -> list[dict[str, Any]]:
    if not CONF.is_file() or not CONVERT_CONTROLS.is_file():
        pytest.skip("submodule未取得(CIではsoramimic本体がprivateのため取得しない)")

    def flatten(items: list) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for w in items or []:
            if not isinstance(w, dict):
                continue
            if isinstance(w.get("items"), list):
                out.extend(flatten(w["items"]))
            else:
                out.append(w)
        return out

    conf = json.loads(CONF.read_text(encoding="utf-8"))
    entries = [
        w
        for w in flatten(conf.get("wordlist", []))
        if w.get("filepath") and w.get("value") != "ORIGINAL" and w.get("active") is not False
    ]
    assert entries, "confに単語リストがありません"
    return entries


def _run_node(script: str, tmp_path: Path) -> Any:
    if not shutil.which("node"):
        pytest.skip("nodeが無い(editorのJSを実際に走らせて形を突き合わせるため必要)")
    path = tmp_path / "facets.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def _index_facet_default_where() -> str:
    """index.html から facetDefaultWhere の実装をそのまま取り出す。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    head = "function facetDefaultWhere(g) {"
    body = html.split(head)[1].split("\n}")[0]
    return head + body + "\n}"


def _cross_check(tmp_path: Path) -> list[dict[str, Any]]:
    """editorのJSとindex.htmlのJSを実際に走らせ、各リストの式を集める。"""
    entries = _conf_entries()
    script = (
        DOM_SHIM
        + f'const m = await import("file://{CONVERT_CONTROLS}");\n'
        + _index_facet_default_where()
        + "\n"
        + f"const entries = {json.dumps(entries, ensure_ascii=False)};\n"
        + """
const out = entries.map((e) => {
	// editor: 既定チェックのまま compileWhere(=セットアップ画面を触らずに変換)
	const c = document.createElement("div");
	m.renderFacets(c, e);
	const editor = m.compileWhere(c, e);
	// editor: その式をホストから渡されたとみなして復元 → もう一度組み直す
	m.restoreFacets(c, editor);
	const restored = m.compileWhere(c, e);
	// video のトップ画面(選択肢は conf の同じエントリ1件)
	const index = facetDefaultWhere({ entries: [e] });
	return { value: e.value, editor, restored, index };
});
console.log(JSON.stringify(out));
"""
    )
    return _run_node(script, tmp_path)


def test_index_html_and_editor_and_server_agree(tmp_path):
    """3実装が同じ where 文字列を作る(1文字まで)。"""
    rows = _cross_check(tmp_path)
    by_value = {w["value"]: w for w in _conf_entries()}
    assert len(rows) == len(by_value)
    for row in rows:
        entry = by_value[row["value"]]
        expected = default_where(entry)
        assert row["editor"] == expected, (
            f"{row['value']}: editorの式とサーバーの式が違う\n"
            f"  editor: {row['editor']}\n  server: {expected}"
        )
        assert row["index"] == expected, (
            f"{row['value']}: index.htmlの式とサーバーの式が違う\n"
            f"  index.html: {row['index']}\n  server: {expected}"
        )


def test_editor_restores_the_same_filter_from_the_where(tmp_path):
    """editorの restoreFacets が同じチェック状態(=同じ式)に戻せる。

    ここが崩れると、video が渡した絞り込みが editor の中で静かに消える。
    """
    rows = _cross_check(tmp_path)
    for row in rows:
        assert row["restored"] == row["editor"], (
            f"{row['value']}: restoreFacets で絞り込みが変わった\n"
            f"  渡した式: {row['editor']}\n  復元後  : {row['restored']}"
        )


def test_server_predicts_the_editor_roundtrip():
    """facets.restored_where が editor の往復を正しく予測している。

    :func:`test_editor_restores_the_same_filter_from_the_where` が editor 実物で
    見ている往復を、サーバー側の予測(シードにwhereを載せてよいかの判定)でも
    同じ結果にする。表せない形の where は載せない、も併せて固定する。
    """
    for entry in _conf_entries():
        where = default_where(entry)
        assert restored_where(entry, where) == where
        assert survives_editor_facets(entry, where)
        # 絞り込み無し(None)は載せるものが無いので渡さない
        assert not survives_editor_facets(entry, None)
        # 旧video形(値ごとの括弧なし)はチェックに一致せず、絞り込みが消える
        flat = where.replace("(", "").replace(")", "")
        if flat != where:
            assert restored_where(entry, flat) == ""
            assert not survives_editor_facets(entry, flat)


def _legacy_default_where(entry: dict[str, Any]) -> str:
    """変更前の index.html(facetDefaultWhere)が組んでいた式。

    値ごと・ファセットごとの括弧が無く、全値が既定ONのファセットは節を出さない。
    行集合が変わっていないことを確かめるためだけに残す(復元はできない形)。
    """
    facets = entry.get("facets") or []
    if not facets:
        return str(entry.get("where") or "")
    clauses = []
    for f in facets:
        values = f.get("values") or []
        on = [v for v in values if v.get("default") is True]
        if not on or len(on) == len(values):
            continue
        cols = f.get("columns") or ([f["column"]] if f.get("column") else [])
        preds: list[str] = []
        for v in on:
            preds += [v["where"]] if v.get("where") else [f"{c}={v['v']}" for c in cols]
        if preds:
            clauses.append(" or ".join(preds))
    if len(clauses) == 1:
        return clauses[0]
    return " and ".join("(" + c + ")" for c in clauses)


def _select(csv_path: Path, where: str) -> list[tuple[str, ...]]:
    """エンジンと同じ where パーサでCSVを絞り込む(soramimicのParser)。"""
    from soramimic.word_list import Parser

    text = csv_path.read_text(encoding="utf-8")
    # loadDatabaseCsvText と同じ前処理(カンマ前後の空白を落とす)
    text = text.replace(" ,", ",").replace(", ", ",")
    lines = text.replace("\r\n", "\n").split("\n")
    header, rows = lines[0].split(","), [ln.split(",") for ln in lines[1:] if ln]
    if not where:
        return [tuple(r) for r in rows]
    return [tuple(r) for r in Parser().filter(where, header, rows)]


def test_new_where_selects_the_same_rows_as_the_old_one():
    """名前付きリスト全18件で、変更前後の where が同じ行を選ぶ。

    形をそろえるにあたって、括弧を足したこと・全値ONのファセットも節に
    することにしたことで**出力の中身が変わっていない**ことを固定する
    (全値ONの節は「列の値がその一覧のどれか」という条件になるので、
    一覧に無い値の行があれば結果が変わりうる)。
    """
    if not (WORDLISTS / "baseball.csv").is_file():
        pytest.skip("submodule未取得(CIではsoramimic本体がprivateのため取得しない)")
    entries = _conf_entries()
    assert len(entries) == 18, f"conf の単語リスト数が変わった: {len(entries)}件"
    for entry in entries:
        csv_path = WORDLISTS / Path(entry["filepath"]).name
        old, new = _legacy_default_where(entry), default_where(entry)
        assert _select(csv_path, old) == _select(csv_path, new), (
            f"{entry['value']}: 絞り込みの結果が変わった\n  旧: {old}\n  新: {new}"
        )


def test_default_where_only_applies_to_the_packaged_wordlists(tmp_path):
    """既定の絞り込みは同梱リストにだけ当てる(同名の手元CSVには当てない)。

    conf の既定は type・status といった列を前提にするので、たまたま同じ
    ファイル名のCSVに当てると絞り込みが空振りする(列が無い)。
    """
    from soramimic_video.convert import WORDLISTS_DIR, resolve_convert_settings

    entries = _conf_entries()
    name = Path(entries[0]["filepath"]).stem
    packaged = WORDLISTS_DIR / f"{name}.csv"
    assert resolve_convert_settings(packaged)[0] == default_where(entries[0])
    # 同じ名前でも別の場所のCSVは素通し(絞り込みなし)
    local = tmp_path / f"{name}.csv"
    local.write_text("id,original,surface,pronunciation\n0,あ,あ,ア", encoding="utf-8")
    assert resolve_convert_settings(local)[0] is None
    # 明示指定した where はどちらでもそのまま
    assert resolve_convert_settings(packaged, "((type=nick))")[0] == "((type=nick))"
