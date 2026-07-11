"""image_credit.py のテスト: Commonsのクレジット取得とキャッシュ。"""

import json
from pathlib import Path

from soramimic_video import image_credit
from soramimic_video.image_credit import (
    commons_file_title,
    credit_from_extmetadata,
    fetch_image_credit,
)


def test_commons_file_title_from_filepath_url():
    url = "http://commons.wikimedia.org/wiki/Special:FilePath/Aioi%20Station%20J09.jpg"
    assert commons_file_title(url) == "File:Aioi Station J09.jpg"
    # サムネイル指定(?width)は落とす
    assert commons_file_title(url + "?width=1200") == "File:Aioi Station J09.jpg"


def test_commons_file_title_prefers_image_page():
    page = "https://commons.wikimedia.org/wiki/File:Aioi_Station_J09.jpg"
    assert commons_file_title("", page) == "File:Aioi_Station_J09.jpg"


def test_commons_file_title_non_commons_is_none():
    assert commons_file_title("https://example.com/a.jpg") is None
    assert commons_file_title("/local/path.jpg", "") is None
    assert commons_file_title("") is None


def test_credit_from_extmetadata_attribution_required():
    meta = {
        "Artist": {"value": '<a href="https://example.com">山田 太郎</a>'},
        "LicenseShortName": {"value": "CC BY-SA 4.0"},
        "AttributionRequired": {"value": "true"},
    }
    info = credit_from_extmetadata(meta)
    assert info["artist"] == "山田 太郎"  # HTMLタグは落とす
    assert info["credit_text"] == "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"
    assert info["attribution_required"] is True


def test_credit_from_extmetadata_not_required_is_empty_text():
    meta = {
        "Artist": {"value": "山田 太郎"},
        "LicenseShortName": {"value": "Public domain"},
        "AttributionRequired": {"value": "false"},
    }
    info = credit_from_extmetadata(meta)
    assert info["credit_text"] == ""  # PD等は表記不要 → フレームに描かれない
    assert info["license"] == "Public domain"


def test_credit_from_extmetadata_missing_fields():
    # Artist不明でもライセンスと出典だけで文言を作る
    info = credit_from_extmetadata({"LicenseShortName": {"value": "CC BY 2.0"}})
    assert info["credit_text"] == "CC BY 2.0, via Wikimedia Commons"
    # 全部欠けていても安全側(表記必要扱い)で出典だけ残す
    assert credit_from_extmetadata({})["credit_text"] == "via Wikimedia Commons"


def test_credit_from_extmetadata_truncates_long_artist():
    info = credit_from_extmetadata({"Artist": {"value": "あ" * 100}})
    assert len(info["artist"]) <= 40
    assert info["artist"].endswith("…")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_api(monkeypatch, calls: list):
    payload = {
        "query": {
            "pages": [
                {
                    "imageinfo": [
                        {
                            "extmetadata": {
                                "Artist": {"value": "山田 太郎"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "AttributionRequired": {"value": "true"},
                            }
                        }
                    ]
                }
            ]
        }
    }

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(payload)

    monkeypatch.setattr(image_credit.requests, "get", fake_get)


def test_fetch_image_credit_caches(tmp_path: Path, monkeypatch):
    calls: list = []
    _fake_api(monkeypatch, calls)
    url = "http://commons.wikimedia.org/wiki/Special:FilePath/A.jpg"
    info = fetch_image_credit(url, "", tmp_path)
    assert info is not None
    assert info["credit_text"] == "山田 太郎, CC BY-SA 4.0, via Wikimedia Commons"
    assert len(calls) == 1
    # 2回目はキャッシュから(APIを叩かない)
    again = fetch_image_credit(url, "", tmp_path)
    assert again == info
    assert len(calls) == 1
    cached = list((tmp_path / "credits").glob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text(encoding="utf-8")) == info


def test_fetch_image_credit_non_commons_skips_network(tmp_path: Path, monkeypatch):
    calls: list = []
    _fake_api(monkeypatch, calls)
    assert fetch_image_credit("https://example.com/a.jpg", "", tmp_path) is None
    assert calls == []


def test_fetch_image_credit_failure_not_cached(tmp_path: Path, monkeypatch):
    import requests

    def fail_get(url, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(image_credit.requests, "get", fail_get)
    url = "http://commons.wikimedia.org/wiki/Special:FilePath/A.jpg"
    assert fetch_image_credit(url, "", tmp_path) is None
    assert not (tmp_path / "credits").exists()  # 失敗はキャッシュしない(次回再試行)
