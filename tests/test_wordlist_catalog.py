import json

from soramimic_video.api import LAUNCH_CATALOG_PATH, load_launch_catalog
from soramimic_video.layout import builtin_layout_names
from soramimic_video.wordlist_catalog import (
    WORDLIST_CATALOG_PATH,
    default_launch_wordlists,
    load_wordlist_catalog,
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
