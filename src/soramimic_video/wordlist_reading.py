"""Shared reading provider for uploaded and packaged word lists."""

from __future__ import annotations


def wordlist_reading(text: str) -> str:
    from soramimic import resolve_wordlist_reading
    from soramimic_yomi import get_yomi

    return resolve_wordlist_reading(text, get_yomi)
