"""Public image attribution shared by video exports and source views."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

FIELDS = ("id", "original", "image", "image_page", "image_credit", "image_terms_page",
          "image_usage", "org")


def distinct_credits(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for row in rows:
        item = {key: str(row.get(key) or "") for key in FIELDS}
        item["original"] = item["original"] or str(row.get("surface") or row.get("word") or "")
        item["image_credit"] = item["image_credit"] or str(row.get("credit") or "")
        key = tuple(item.values())
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def credits_text(rows: list[dict[str, Any]]) -> str:
    blocks = []
    for item in distinct_credits(rows):
        blocks.append("\n".join([
            item["original"],
            "クレジット: " + (item["image_credit"] or "未記載（掲載元で確認してください）"),
            "画像掲載元: " + (item["image_page"] or "未記載"),
            "公式規約: " + (item["image_terms_page"] or "未記載（掲載元で確認してください）"),
            "利用区分: " + (item["image_usage"] or "指定なし（掲載元の条件を確認）"),
            "画像: " + item["image"],
        ]))
    return "\n\n".join(blocks)


def write_credit_files(rows: list[dict[str, Any]], work: Path) -> Path:
    items = distinct_credits(rows)
    (work / "credits.json").write_text(json.dumps(
        {"items": items, "text": credits_text(items)}, ensure_ascii=False,
    ), encoding="utf-8")

    def cell(value: str) -> str:
        return escape(value, quote=False).replace("|", "&#124;").replace("\r", "").replace(
            "\n", "<br>"
        ).replace("`", "&#96;").replace("[", "&#91;").replace(
            "]", "&#93;"
        ).replace("\\", "&#92;")

    lines = ["# この動画で使用した画像の出典・クレジット", "",
             "公開時は各画像の掲載元・公式規約の条件をご確認ください。",
             "未記載の項目は、利用制限やクレジットが不要であることを意味しません。", "",
             f"使用素材：{len(items)}件", "",
             "| 人物・素材 | 画像 | クレジット | 画像掲載元 | 公式規約 | 利用区分 |",
             "|---|---|---|---|---|---|"]
    for item in items:
        lines.append("| " + " | ".join(cell(item[key]) for key in (
            "original", "image", "image_credit", "image_page", "image_terms_page", "image_usage"
        )) + " |")
    path = work / "credits.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
