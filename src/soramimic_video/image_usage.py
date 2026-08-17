"""単語画像に付いた用途制限を安全側で判定する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NONCOMMERCIAL_FANWORK = "noncommercial_fanwork"


def require_image_usage(
    row: Mapping[str, Any], *, allow_noncommercial_fanwork: bool = False
) -> None:
    """行の画像用途を検査する。未知の非空値も黙って利用しない。"""
    usage = str(row.get("image_usage") or "").strip()
    if not usage:
        return
    if usage == NONCOMMERCIAL_FANWORK:
        if allow_noncommercial_fanwork:
            return
        terms = str(row.get("image_terms_page") or "").strip()
        suffix = f" 条件: {terms}" if terms else ""
        raise ValueError(
            "この単語には非営利ファン活動に限定された画像が含まれます。"
            "利用条件を確認し、非営利ファンワークモードを明示的に有効にしてください。"
            + suffix
        )
    raise ValueError(f"未対応の画像利用区分です: {usage}")


def image_usage_allowed(
    row: Mapping[str, Any] | None, *, allow_noncommercial_fanwork: bool = False
) -> bool:
    """プレビュー等で、画像を取得してよい行かを返す。"""
    if row is None:
        return True
    try:
        require_image_usage(
            row, allow_noncommercial_fanwork=allow_noncommercial_fanwork
        )
    except ValueError:
        return False
    return True
