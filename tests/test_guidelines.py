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
    assert "利用ガイドライン" in response.text
    assert "動画に含まれる画像・キャラクターなどの利用条件をご確認ください。" in response.text
    assert response.text.count('href="https://example.com/shared"') == 1
    assert "規約1" in response.text and "規約2" in response.text
    assert 'href="/"' in response.text


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
    external = [link for link in parsed.links if link["href"] != "/"]
    assert external == [{"href": url, "target": "_blank", "rel": "noopener noreferrer"}]


def test_guidelines_tolerate_an_empty_catalog(client):
    browser, policies = client
    policies.clear()
    response = browser.get("/guidelines")
    assert response.status_code == 200
    assert "利用ガイドライン" in response.text
    assert '<ul class="guidelines">' not in response.text
