import json

from soramimic_video.api import LAUNCH_CATALOG_PATH, load_launch_catalog
from soramimic_video.layout import builtin_layout_names
from soramimic_video.wordlist_catalog import (
    WORDLIST_CATALOG_PATH,
    default_launch_wordlists,
    load_wordlist_catalog,
    load_wordlist_image_policies,
)


def test_launch_wordlists_have_complete_video_metadata():
    catalog = load_wordlist_catalog()
    layouts = set(builtin_layout_names())
    launch = default_launch_wordlists()

    assert launch
    assert "marine_life" in launch
    assert "pokemon" in launch
    for name in launch:
        entry = catalog[name]
        assert entry.get("layout") in layouts, f"{name} の既定レイアウトが不正です"
        assert entry.get("phrase"), f"{name} の表示用フレーズがありません"


def test_launch_catalog_does_not_duplicate_wordlist_configuration():
    raw = json.loads(LAUNCH_CATALOG_PATH.read_text(encoding="utf-8"))
    assert "wordlists" not in raw
    assert load_launch_catalog()["wordlists"] == default_launch_wordlists()


def test_wordlist_catalog_is_packaged_next_to_code():
    assert WORDLIST_CATALOG_PATH.is_file()


def test_youtuber_catalog_exposes_noncommercial_image_policy():
    policy = load_wordlist_catalog()["youtuber"]["image_policy"]
    assert policy == {
        "usage": "noncommercial_fanwork",
        "terms": "https://hololivepro.com/terms/",
    }


def test_image_policy_collects_distinct_terms_from_restricted_rows(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({
            "people": {
                "image_policy": {
                    "usage": "noncommercial_fanwork",
                    "terms": "https://fallback.example/guidelines",
                },
            },
        }),
        encoding="utf-8",
    )
    wordlists = tmp_path / "wordlists"
    wordlists.mkdir()
    (wordlists / "people.csv").write_text(
        "image_usage,image_terms_page\n"
        "noncommercial_fanwork,https://www.anycolor.co.jp/guidelines/\n"
        "noncommercial_fanwork,https://www.anycolor.co.jp/guidelines/\n"
        "noncommercial_fanwork,https://hololivepro.com/terms/\n"
        "other,https://ignored.example/guidelines\n"
        "noncommercial_fanwork,javascript:alert(1)\n",
        encoding="utf-8",
    )

    policy = load_wordlist_image_policies(wordlists, catalog)["people"]

    assert policy["terms_pages"] == [
        {
            "url": "https://www.anycolor.co.jp/guidelines/",
            "label": "ANYCOLOR二次創作ガイドライン",
        },
        {
            "url": "https://hololivepro.com/terms/",
            "label": "ホロライブプロダクション二次創作ガイドライン",
        },
    ]


def test_image_policy_uses_safe_catalog_fallback_when_csv_is_missing(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({
            "people": {
                "image_policy": {
                    "usage": "noncommercial_fanwork",
                    "terms": "https://example.com/guidelines",
                },
            },
        }),
        encoding="utf-8",
    )

    policy = load_wordlist_image_policies(tmp_path / "missing", catalog)["people"]

    assert policy["terms_pages"] == [{
        "url": "https://example.com/guidelines",
        "label": "example.com 二次創作ガイドライン",
    }]
