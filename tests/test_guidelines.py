"""Public guideline links follow the existing wordlist policies."""

from html.parser import HTMLParser

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from soramimic_video import api as api_mod  # noqa: E402


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


@pytest.fixture
def client(tmp_path, monkeypatch):
    policies = {
        "first": {"terms_pages": [
            {"url": "https://example.com/one", "label": "規約1"},
            {"url": "https://example.com/shared", "label": "共通規約"},
        ]},
        "second": {"terms_pages": [
            {"url": "https://example.com/two", "label": "規約2"},
            {"url": "https://example.com/shared", "label": "共通規約"},
        ]},
        "empty": {"terms_pages": []},
    }
    monkeypatch.setattr(api_mod, "load_wordlist_image_policies", lambda root: policies)
    monkeypatch.setenv(api_mod.API_KEY_ENV, "test-secret")
    return TestClient(api_mod.create_app(jobs_dir=tmp_path / "jobs")), policies


def test_guidelines_are_public_and_deduplicate_existing_terms(client):
    browser, _ = client
    assert browser.get("/api/jobs").status_code == 401
    response = browser.get("/guidelines")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<h1 id="guidelines-title">利用ガイドライン</h1>' in response.text
    assert '<nav aria-label="目次">' in response.text
    assert '<a href="#generation-tips">生成しやすい曲のヒント</a>' in response.text
    assert '<a href="#image-guidelines-title">画像の利用ガイドライン</a>' in response.text
    assert '<a href="#contact-title">画像の権利をお持ちの方へ</a>' in response.text
    assert response.text.count('href="https://example.com/shared"') == 1
    assert "規約1" in response.text and "規約2" in response.text
    assert 'href="/"' in response.text
    assert '<h2 id="generation-tips">生成しやすい曲のヒント</h2>' in response.text
    assert "ボーカルがはっきり聞こえる、日本語のソロ歌唱曲" in response.text


@pytest.mark.parametrize("wordlist", ["first", "second", "unknown", "empty", ""])
def test_guidelines_filter_or_fall_back_to_all_terms(client, wordlist):
    browser, _ = client
    response = browser.get("/guidelines", params={"wordlist": wordlist})
    assert response.status_code == 200
    assert ("規約1" in response.text) == (wordlist != "second")
    assert ("規約2" in response.text) == (wordlist != "first")
    assert "共通規約" in response.text


def test_guidelines_escape_links_and_reject_unsafe_schemes(client):
    browser, policies = client
    url = 'https://example.com/terms?a="quoted"&b=<value>'
    policies["unsafe"] = {"terms_pages": [
        {"url": url, "label": '<script>alert("label")</script>'},
        {"url": "javascript:alert(1)", "label": "JavaScript link"},
        {"url": "data:text/html,test", "label": "Data link"},
        {"url": "https://[invalid", "label": "Invalid link"},
    ]}
    response = browser.get("/guidelines", params={"wordlist": "unsafe"})
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "JavaScript link" not in response.text
    assert "Data link" not in response.text
    assert "Invalid link" not in response.text
    parsed = Links()
    parsed.feed(response.text)
    external = [
        link for link in parsed.links
        if link["href"].startswith(("http://", "https://"))
    ]
    assert {"href": url, "target": "_blank", "rel": "noopener noreferrer"} in external
    assert all(link["target"] == "_blank" and link["rel"] == "noopener noreferrer"
               for link in external)


def test_guidelines_tolerate_an_empty_catalog(client):
    browser, policies = client
    policies.clear()
    response = browser.get("/guidelines")
    assert response.status_code == 200
    assert '<h1 id="guidelines-title">利用ガイドライン</h1>' in response.text
    assert '<ul class="guidelines">' not in response.text


def test_public_guidelines_explain_data_handling(client, monkeypatch):
    browser, _ = client
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.JOB_TTL_HOURS_ENV, "24")

    response = browser.get("/guidelines")

    assert '<h2 id="data-handling-title">データの取り扱い</h2>' in response.text
    assert "元の音源・歌詞と解析用データは処理終了時に削除し、" in response.text
    assert "失敗した場合は自動で再試行します。" in response.text
    assert "完成動画は24時間の保存期間を過ぎたものから" in response.text
    assert "定期的に自動削除します。" in response.text
    assert "入力内容をAIモデルの学習には使用しません。" in response.text
    assert '<a href="#data-handling-title">データの取り扱い</a>' in response.text
    assert response.text.index('id="guidelines-title"') < response.text.index(
        'id="data-handling-title"'
    )


def test_public_guidelines_limit_uploads_and_outputs_to_private_use(client, monkeypatch):
    browser, _ = client
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")

    response = browser.get("/guidelines")

    assert '<h2 id="usage-scope-title">楽曲・生成物の利用範囲</h2>' in response.text
    assert "個人・家庭内など限られた範囲で、仕事以外の私的利用" in response.text
    assert "SNSへの投稿・公開・配布などは私的利用には含まれません。" in response.text
    assert "必要な許諾・利用条件を別途確認できたもの" in response.text
    assert '<a href="#usage-scope-title">楽曲・生成物の利用範囲</a>' in response.text
    assert 'href="https://www.bunka.go.jp/seisaku/chosakuken/taisetsu/point/"' in response.text


def test_image_sections_make_their_scope_explicit(client):
    browser, _ = client
    response = browser.get("/guidelines")
    assert '<h2 id="image-guidelines-title">画像の利用ガイドライン</h2>' in response.text
    assert '<h2 id="contact-title">画像の権利をお持ちの方へ</h2>' in response.text
    assert "動画内で使用される画像に関するご連絡・ご要望は、" in response.text


def test_private_guidelines_do_not_claim_public_retention_policy(client):
    browser, _ = client
    response = browser.get("/guidelines")
    assert 'id="data-handling-title"' not in response.text
    assert 'href="#data-handling-title"' not in response.text
    assert 'id="usage-scope-title"' not in response.text
    assert 'href="#usage-scope-title"' not in response.text


def test_guidelines_keep_distinct_terms_urls(client):
    browser, policies = client
    policies["people"] = {"terms_pages": [
        {"url": "https://note.com/a/terms", "label": "活動名A 利用ガイドライン",
         "people": ["活動名A", "A別名"]},
        {"url": "https://note.com/b/terms", "label": "活動名B 利用ガイドライン",
         "people": ["活動名B"]},
        {"url": "https://note.com/a/terms/en", "label": "活動名A English",
         "people": ["活動名A"]},
    ]}
    response = browser.get("/guidelines", params={"wordlist": "people"})
    parser = Links()
    parser.feed(response.text)
    urls = [item["href"] for item in parser.links]
    assert "https://note.com/a/terms" in urls
    assert "https://note.com/a/terms/en" in urls
    assert "https://note.com/b/terms" in urls
