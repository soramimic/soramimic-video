"""Summarize usage notices among the rows eligible for lyric conversion."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .wordlist_catalog import GUIDELINE_LABELS


def _terms_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(c.isspace() for c in value)):
            return ""
    except ValueError:
        return ""
    return value


def _summarize(reader: Iterable[list[str]], where: str) -> dict[str, Any]:
    iterator = iter(reader)
    header = next(iterator, [])
    empty: dict[str, Any] = {"required": False, "terms": []}
    # Most lists have no notice metadata; reading their bodies is unnecessary.
    if not {"usage_notice", "image_usage"}.intersection(header):
        return empty
    notice_columns = [header.index(column) for column in ("usage_notice", "image_usage")
                      if column in header]
    rows = [row for row in iterator if any(
        index < len(row) and row[index].strip() not in {"", "NA"}
        for index in notice_columns)]
    if not rows:
        return empty
    if where:
        from soramimic.word_list import Parser

        try:
            filtered = Parser().filter(where, header, rows)
        except (ValueError, IndexError, RecursionError) as exc:
            raise ValueError("絞り込み条件を確認できませんでした") from exc
        if not isinstance(filtered, list):
            raise ValueError("絞り込み条件を確認できませんでした")
        rows = filtered
    required = False
    terms: dict[str, dict[str, str]] = {}
    for values in rows:
        row = dict(zip(header, values, strict=False))
        for flag, link in (("usage_notice", "usage_terms_page"),
                           ("image_usage", "image_terms_page")):
            if row.get(flag, "").strip() in {"", "NA"}:
                continue
            required = True
            url = _terms_url(row.get(link, ""))
            if url:
                terms.setdefault(url, {
                    "url": url,
                    "label": GUIDELINE_LABELS.get(
                        url, f"{urlsplit(url).hostname} 利用ガイドライン"),
                })
    return {"required": required, "terms": list(terms.values())}


def summarize_csv_usage(text: str, where: str = "") -> dict[str, Any]:
    """Inspect a normalized custom CSV without persisting it or fetching its URLs."""
    return _summarize(csv.reader(io.StringIO(text)), where)


def summarize_file_usage(path: Path, where: str = "") -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return _summarize(csv.reader(handle), where)
