"""Self-contained text cards for imported lyrics with an unavailable wordlist."""

from __future__ import annotations

import copy
import csv
import io
from typing import Any

from .wordlist_csv import parse_editor_text


def is_text_only(payload: dict[str, Any]) -> bool:
    return (payload.get("wordlist") or {}).get("textOnly") is True


def text_only_import(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the selected words/readings, and use only their text as list data.

    This is a snapshot of the selected words, not a dictionary for generating new
    lyrics. Require complete word coverage so importing cannot silently omit or
    regenerate a part of the song. Fillers are already selected words too.
    """
    rows = []
    seen = set()
    for words, units in zip(payload["results"], payload["unitsList"], strict=True):
        cursor = 0
        for word in words:
            surface, kana = word.get("surface"), word.get("kana")
            if (not isinstance(surface, str) or not surface.strip()
                    or not isinstance(kana, str) or not kana.strip()):
                raise ValueError("画像なしで動画にするには、各単語の表記と読みが必要です")
            period = word.get("period")
            if (not isinstance(period, list) or len(period) != 2
                    or not all(type(value) is int for value in period)
                    or period[0] != cursor or not cursor < period[1] <= len(units)):
                raise ValueError(
                    "替え歌が未完成です。ソラミミックで全行を変換してから送ってください"
                )
            cursor = period[1]
            word_id = word.get("id", "")
            if not isinstance(word_id, (str, int)):
                raise ValueError("歌詞JSONの単語IDが不正です")
            # Keep IDs and aliases; do not import image URLs or arbitrary columns.
            row = (str(word_id), surface, surface, kana)
            if row not in seen:
                seen.add(row)
                rows.append(row)
        if cursor != len(units):
            raise ValueError("替え歌が未完成です。ソラミミックで全行を変換してから送ってください")
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(("id", "original", "surface", "pronunciation"))
    writer.writerows(rows)
    csv_text = parse_editor_text(stream.getvalue()).text
    entry = payload.get("wordlist") or {}
    label = entry.get("text")
    label = label.strip()[:100] if isinstance(label, str) and label.strip() else "歌詞の単語"
    result = copy.deepcopy(payload)
    result["wordlist"] = {
        "value": "ORIGINAL", "text": label, "csvText": csv_text, "textOnly": True,
    }
    result.pop("where", None)
    return result
