"""Word metadata and the actual conversion filter determine the notice."""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from soramimic_video.usage_notices import summarize_csv_usage, summarize_file_usage

HEADER = ["id", "original", "surface", "pronunciation", "kind", "usage_notice",
          "usage_terms_page", "image_usage", "image_terms_page"]
ROWS = [
    ["1", "通常語", "通常語", "ツウジョウゴ", "ordinary", "", "", "", ""],
    ["2", "注意語", "注意語", "チュウイゴ", "person", "guidelines",
     "https://example.com/person", "", ""],
    ["2", "注意語", "別名", "ベツメイ", "person", "guidelines",
     "https://example.com/person", "", ""],
    ["3", "画像語", "画像語", "ガゾウゴ", "picture", "", "",
     "noncommercial_fanwork", "https://example.com/image"],
]


def csv_text(rows=ROWS):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue().rstrip("\n")


def test_notices_follow_filtered_rows_and_deduplicate_terms():
    text = csv_text()
    assert summarize_csv_usage(text, "kind=ordinary") == {"required": False, "terms": []}
    assert summarize_csv_usage(text, "kind=missing") == {"required": False, "terms": []}
    assert summarize_csv_usage(text, "(kind=person) and (id=9)")["required"] is False
    person = summarize_csv_usage(text, "kind~=pers")
    assert person["required"] is True
    assert [t["url"] for t in person["terms"]] == ["https://example.com/person"]
    assert summarize_csv_usage(text, "kind=picture")["required"] is True
    both = summarize_csv_usage(text, "kind!=ordinary")
    assert {t["url"] for t in both["terms"]} == {
        "https://example.com/person", "https://example.com/image",
    }


def test_missing_columns_and_missing_or_unsafe_urls():
    assert summarize_csv_usage("id,surface\n1,通常語") == {"required": False, "terms": []}
    for url in ("", "javascript:alert(1)", "https://[invalid", "https://a:b@example.com"):
        row = [*ROWS[1]]
        row[6] = url
        assert summarize_csv_usage(csv_text([row])) == {"required": True, "terms": []}
    row = [*ROWS[1]]
    row[7:] = ["noncommercial_fanwork", row[6]]
    assert len(summarize_csv_usage(csv_text([row]))["terms"]) == 1


def test_packaged_scientists_use_notice_metadata_after_filtering():
    from soramimic_video.convert import WORDLISTS_DIR, default_where

    path = WORDLISTS_DIR / "scientist.csv"
    assert "usage_notice" in path.read_text(encoding="utf-8").splitlines()[0]
    assert summarize_file_usage(path, default_where("scientist") or "")["required"] is False
    assert summarize_file_usage(path, "celebrity_doctorate=yes")["required"] is True
    assert summarize_file_usage(path, "celebrity_doctorate=yes and id=does-not-exist")[
        "required"] is False
    vtuber = WORDLISTS_DIR / "vtuber.csv"
    assert summarize_file_usage(vtuber)["required"] is True
    assert summarize_file_usage(vtuber, "id=does-not-exist")["required"] is False


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from soramimic_video import api, convert

    root = tmp_path / "lists"
    root.mkdir()
    (root / "mixed.csv").write_text(csv_text(), encoding="utf-8")
    monkeypatch.setattr(convert, "WORDLISTS_DIR", root)
    monkeypatch.setattr(api, "launch_wordlist_names", lambda: {"mixed"})
    monkeypatch.setenv(api.API_KEY_ENV, "test-key")
    return TestClient(api.create_app(jobs_dir=tmp_path / "jobs"))


def post(client, **data):
    return client.post("/api/wordlist-usage", data=data, headers={"X-API-Key": "test-key"})


def test_api_supports_any_packaged_name_and_custom_metadata(client):
    assert client.post("/api/wordlist-usage", data={"wordlist": "mixed"}).status_code == 401
    assert post(client, wordlist="mixed", where="kind=ordinary").json()["required"] is False
    assert post(client, wordlist="mixed", where="kind=person").json()["required"] is True
    custom = post(client, wordlist_text=csv_text(), where="kind=picture")
    assert custom.status_code == 200
    assert custom.json()["terms"][0]["url"] == "https://example.com/image"
    assert post(client, wordlist="mixed", wordlist_text=csv_text()).status_code == 400
    assert post(client, wordlist="missing").status_code == 404
    assert post(client, wordlist="mixed", where="absent=value").status_code == 400
    assert post(client, wordlist="mixed", where="(" * 4200).status_code == 422


def test_public_allowlist_and_simple_ui_custom_restriction(client, monkeypatch):
    from soramimic_video import api

    monkeypatch.setenv(api.PUBLIC_ENV, "1")
    assert post(client, wordlist="../private.csv").status_code == 404
    assert post(client, wordlist="mixed").status_code == 200
    monkeypatch.setenv(api.SIMPLE_UI_ENV, "1")
    assert post(client, wordlist_text=csv_text()).status_code == 404
    assert post(client, wordlist="mixed").status_code == 200


def test_custom_upload_limits_apply_to_notice_lookup(client, monkeypatch):
    from soramimic_video import wordlist_csv

    monkeypatch.setenv(wordlist_csv.MAX_BYTES_ENV, "20")
    response = post(client, wordlist_text=csv_text())
    assert response.status_code == 400
    response = client.post("/api/wordlist-usage", headers={"X-API-Key": "test-key"},
                           files={"wordlist_text": ("words.csv", csv_text().encode(), "text/csv")})
    assert response.status_code == 400


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for UI behavior test")
def test_ui_uses_generic_results_and_ignores_stale_responses():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/soramimic_video/static/index.html").read_text(encoding="utf-8")
    functions = "let wordlistUsageRequest" + html.split("let wordlistUsageRequest", 1)[1].split(
        "// カード下部の案内", 1)[0]
    script = """
const assert = require("node:assert/strict");
class El {
  constructor() { this.children = []; this.value = ""; this.hidden = true; }
  replaceChildren(...children) { this.children = children; }
  appendChild(child) { this.children.push(child); }
}
const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, new El());
  return elements.get(id);
};
const document = { createElement: () => new El() };
let name = "arbitrary-list", custom = null, registeredCustom = null;
const currentWordlistName = () => name;
const currentPreviewCustomList = () => custom;
const activeCustomList = () => registeredCustom;
const apiKey = () => "", headers = () => ({});
const setCustomWordlistText = (form, text) => form.set("wordlist_text", text);
const requests = [];
const fetch = (url, options) => new Promise((resolve, reject) => {
  assert.equal(url, "/api/wordlist-usage");
  requests.push({ options, resolve, reject });
});
const complete = (index, required, terms = []) => requests[index].resolve({
  ok: true, json: async () => ({ required, terms }),
});
""" + functions + """
(async () => {
  $("where").value = "kind=person";
  const first = updateWordlistUsageNotice();
  assert.equal(requests[0].options.body.get("wordlist"), name);
  assert.equal(requests[0].options.body.get("where"), "kind=person");
  complete(0, true, [
    { url: "https://example.com/terms", label: "<利用条件>" },
    { url: "javascript:alert(1)", label: "unsafe" },
  ]);
  await first;
  assert.equal($("builder-fanwork-notice").hidden, false);
  const links = $("builder-usage-terms").children;
  assert.equal(links.length, 1);
  assert.equal(links[0].children[0].textContent, "<利用条件>");
  assert.equal(links[0].children[0].rel, "noopener noreferrer");
  assert.equal($("builder-usage-links").hidden, false);
  await updateWordlistUsageNotice();
  assert.equal(requests.length, 1, "unchanged polling must reuse the result");

  $("where").value = "kind=picture";
  const oldRequest = updateWordlistUsageNotice();
  $("where").value = "kind=ordinary";
  const currentRequest = updateWordlistUsageNotice();
  complete(2, false);
  await currentRequest;
  complete(1, true);
  await oldRequest;
  assert.equal($("builder-fanwork-notice").hidden, true);
  assert.equal($("builder-usage-links").hidden, true);

  custom = registeredCustom = { text: "id,surface,usage_notice\\n1,注意語,guidelines" };
  const own = updateWordlistUsageNotice();
  assert.equal(requests[3].options.body.get("wordlist"), "");
  assert.equal(requests[3].options.body.get("where"), "");
  assert.equal(requests[3].options.body.get("wordlist_text"), custom.text);
  complete(3, true);
  await own;
  assert.equal($("builder-fanwork-notice").hidden, false);

  registeredCustom = null; // エディタからの自作CSVにはその絞り込みを適用する
  $("where").value = "id=missing";
  const editor = updateWordlistUsageNotice();
  assert.equal(requests[4].options.body.get("where"), "id=missing");
  complete(4, false);
  await editor;
  assert.equal($("builder-fanwork-notice").hidden, true);

  custom = null;
  name = "another-list";
  const failure = updateWordlistUsageNotice();
  requests[5].reject(new Error("offline"));
  await failure;
  assert.equal($("builder-fanwork-notice").hidden, false);
  assert.equal($("builder-usage-status").hidden, false);
  name = "";
  await updateWordlistUsageNotice();
  assert.equal($("builder-fanwork-notice").hidden, true);
  assert.equal(requests.length, 6);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
