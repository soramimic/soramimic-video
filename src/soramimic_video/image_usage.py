"""単語画像に付いた用途制限を安全側で判定する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NONCOMMERCIAL_FANWORK = "noncommercial_fanwork"


class PrivateAssetPolicyError(ValueError):
    """A private logical asset is missing, public, or inconsistent with its row."""


def _private_manifest_usage(row: Mapping[str, Any]) -> tuple[str, str] | None:
    from .asset_store import (
        is_private_asset_id,
        is_public_runtime,
        manifest_entry,
    )

    image = str(row.get("image") or "").strip()
    if not is_private_asset_id(image):
        return None
    if is_public_runtime():
        raise PrivateAssetPolicyError("非公開画像は公開環境では利用できません")
    entry = manifest_entry(image)
    if not isinstance(entry, dict) or entry.get("status") != "available":
        raise PrivateAssetPolicyError("非公開画像がasset storeに登録されていません")
    usage = str(entry.get("image_usage") or "").strip()
    terms = str(entry.get("image_terms_page") or "").strip()
    if not usage or not terms:
        raise PrivateAssetPolicyError("非公開画像の利用条件がasset storeにありません")
    row_usage = str(row.get("image_usage") or "").strip()
    row_terms = str(row.get("image_terms_page") or "").strip()
    if row_usage != usage or row_terms != terms:
        raise PrivateAssetPolicyError("単語リストとasset storeの利用条件が一致しません")
    return usage, terms


def require_image_usage(
    row: Mapping[str, Any], *, allow_noncommercial_fanwork: bool = False
) -> None:
    """行の画像用途を検査する。未知の非空値も黙って利用しない。"""
    private_usage = _private_manifest_usage(row)
    usage = private_usage[0] if private_usage else str(row.get("image_usage") or "").strip()
    if not usage:
        return
    if usage == NONCOMMERCIAL_FANWORK:
        if allow_noncommercial_fanwork:
            return
        terms = (
            private_usage[1]
            if private_usage
            else str(row.get("image_terms_page") or "").strip()
        )
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
