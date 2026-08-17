"""Offline importer for assets that must never be published by an asset host."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from .asset_store import MANIFEST_NAME, PRIVATE_ASSET_PREFIX

MANIFEST_VERSION = 1
MAX_PRIVATE_ASSET_BYTES = 25 * 1024 * 1024
PRIVATE_ID = re.compile(
    r"^asset://private/[a-z0-9][a-z0-9._-]*/"
    r"[a-z0-9][a-z0-9._/-]*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_FIELDS = {
    "id",
    "source_file",
    "source_url",
    "source_page",
    "sha256",
    "credit",
    "usage",
    "terms_page",
    "acquired_at",
    "terms_reviewed_at",
}
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
ALLOWED_USAGE = {"noncommercial_fanwork"}


class PrivateAssetError(ValueError):
    pass


def _iso_date(value: object, field: str) -> str:
    text = str(value or "")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise PrivateAssetError(f"{field} は YYYY-MM-DD で指定してください") from exc
    return text


def _https_url(value: object, field: str) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PrivateAssetError(
            f"{field} は認証情報を含まないHTTPS URLが必要です"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port
    ):
        raise PrivateAssetError(f"{field} は認証情報を含まないHTTPS URLが必要です")
    return text


def _image_info(path: Path) -> tuple[str, str, bool, int, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PrivateAssetError(f"画像を読めません: {path}") from exc
    if size <= 0 or size > MAX_PRIVATE_ASSET_BYTES:
        raise PrivateAssetError(f"画像サイズが不正です: {size} bytes")
    try:
        with Image.open(path) as image:
            image_format = image.format or ""
            width, height = image.size
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise PrivateAssetError(f"画像として認識できません: {path}") from exc
    if image_format not in ALLOWED_FORMATS:
        raise PrivateAssetError(f"未対応の画像形式です: {image_format or 'unknown'}")
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        raise PrivateAssetError(f"画像寸法が不正です: {width}x{height}")
    media_type = Image.MIME.get(image_format, "")
    if not media_type.startswith("image/"):
        raise PrivateAssetError("画像MIMEを判定できません")
    return ALLOWED_FORMATS[image_format], media_type, has_alpha, width, height


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o640)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _existing_assets(store: Path) -> dict[str, dict]:
    manifest = store / MANIFEST_NAME
    if not manifest.exists():
        return {}
    if manifest.is_symlink() or not manifest.is_file():
        raise PrivateAssetError(f"既存manifestは通常ファイルが必要です: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateAssetError(f"既存manifestを読めません: {manifest}") from exc
    raw_assets = data.get("assets") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != MANIFEST_VERSION
        or data.get("private") is not True
        or not isinstance(raw_assets, dict)
    ):
        raise PrivateAssetError("既存manifestは互換性のあるprivate asset storeではありません")
    assets: dict[str, dict] = {}
    for asset_id, entry in raw_assets.items():
        if not isinstance(asset_id, str) or not PRIVATE_ID.fullmatch(asset_id):
            raise PrivateAssetError("既存manifestに不正なprivate asset idがあります")
        if not isinstance(entry, dict):
            raise PrivateAssetError(f"既存manifestのentryが不正です: {asset_id}")
        assets[asset_id] = entry
    return assets


def import_private_assets(input_manifest: Path, store: Path) -> dict[str, int]:
    """Validate and merge local assets without removing other private namespaces."""
    if store.is_symlink():
        raise PrivateAssetError(f"asset storeをsymlinkにはできません: {store}")
    try:
        data = json.loads(input_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateAssetError(f"private asset台帳を読めません: {input_manifest}") from exc
    records = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        raise PrivateAssetError("private asset台帳のassetsは1件以上の配列が必要です")

    assets = _existing_assets(store)
    input_ids: set[str] = set()
    prepared: list[tuple[Path, Path]] = []
    for index, raw in enumerate(records, 1):
        if not isinstance(raw, dict):
            raise PrivateAssetError(f"assets[{index}] はobjectが必要です")
        missing = sorted(REQUIRED_FIELDS - set(raw))
        if missing:
            raise PrivateAssetError(f"assets[{index}] に {missing[0]} がありません")
        asset_id = str(raw["id"]).strip()
        if not PRIVATE_ID.fullmatch(asset_id) or ".." in asset_id.split("/"):
            raise PrivateAssetError(f"assets[{index}] のidが不正です")
        if not asset_id.startswith(PRIVATE_ASSET_PREFIX):
            raise PrivateAssetError(f"assets[{index}] はprivate asset idではありません")
        if asset_id in input_ids:
            raise PrivateAssetError(f"asset idが重複しています: {asset_id}")
        input_ids.add(asset_id)

        source = Path(str(raw["source_file"])).expanduser()
        if source.is_symlink() or not source.is_file():
            raise PrivateAssetError(f"source_fileは通常ファイルが必要です: {source}")
        expected = str(raw["sha256"]).strip()
        if not SHA256.fullmatch(expected) or _sha256(source) != expected:
            raise PrivateAssetError(f"assets[{index}] のsha256が一致しません")
        suffix, media_type, has_alpha, width, height = _image_info(source)
        relative = Path("objects") / expected[:2] / f"{expected}{suffix}"
        destination = store / relative
        prepared.append((source, destination))

        credit = str(raw["credit"]).strip()
        usage = str(raw["usage"]).strip()
        if not credit:
            raise PrivateAssetError(f"assets[{index}] のcreditは空にできません")
        if usage not in ALLOWED_USAGE:
            raise PrivateAssetError(f"assets[{index}] のusageは未対応です: {usage}")
        assets[asset_id] = {
            "status": "available",
            "local_path": relative.as_posix(),
            "sha256": expected,
            "source_url": _https_url(raw["source_url"], "source_url"),
            "source_page": _https_url(raw["source_page"], "source_page"),
            "image_usage": usage,
            "image_terms_page": _https_url(raw["terms_page"], "terms_page"),
            "acquired_at": _iso_date(raw["acquired_at"], "acquired_at"),
            "terms_reviewed_at": _iso_date(
                raw["terms_reviewed_at"], "terms_reviewed_at"
            ),
            "media_type": media_type,
            "has_alpha": has_alpha,
            "width": width,
            "height": height,
            "credit": {
                "status": "known",
                "artist": "",
                "license": "",
                "attribution_required": True,
                "credit_text": credit,
            },
        }

    store.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o750)
    copied = 0
    for source, destination in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise PrivateAssetError(
                f"格納先directoryをsymlinkにはできません: {destination.parent}"
            )
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise PrivateAssetError(f"格納先が通常ファイルではありません: {destination}")
            if _sha256(destination) != _sha256(source):
                raise PrivateAssetError(f"既存objectのhashが不正です: {destination}")
            continue
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            shutil.copyfile(source, tmp)
            os.chmod(tmp, 0o640)
            if _sha256(tmp) != _sha256(source):
                raise PrivateAssetError("private assetコピー後のhashが一致しません")
            os.replace(tmp, destination)
            copied += 1
        finally:
            tmp.unlink(missing_ok=True)

    manifest = {
        "version": MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "private": True,
        "assets": assets,
    }
    _atomic_json(store / MANIFEST_NAME, manifest)
    return {
        "total": len(input_ids),
        "copied": copied,
        "reused": len(input_ids) - copied,
    }
