"""単語リストCSVの画像を画像キャッシュへ事前ダウンロードする(prewarm-images CLI用)。

動画生成(video.build_image_cues)は初回、単語ごとにWikimedia Commonsへ画像と
クレジットを取りに行くためキャッシュが冷えていると時間がかかる。このモジュールは
単語リストCSVを読み、画像URLを直列にゆっくり(--delay)取得してキャッシュを温める。
レート制限に配慮した「事前ウォームアップ」用で、動画生成側のプリフェッチ並列化とは別。
"""

from __future__ import annotations

import csv
import hashlib
import logging
import time
from pathlib import Path

from .image_credit import fetch_image_credit
from .video import download_image

logger = logging.getLogger(__name__)


def _image_cached(url: str, cache_dir: Path) -> bool:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    return any(cache_dir.glob(f"{name}.*"))


def _credit_cached(url: str, cache_dir: Path) -> bool:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    return (cache_dir / "credits" / f"{name}.json").exists()


def _collect_rows(csv_paths: list[Path]) -> dict[str, dict]:
    """CSV群から http(s) の image 列を持つ行をユニークURLごとに集める(初出優先)。"""
    rows: dict[str, dict] = {}
    for path in csv_paths:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                url = (row.get("image") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                rows.setdefault(url, row)
    return rows


def prewarm_images(
    csv_paths: list[Path],
    cache_dir: Path,
    delay: float = 1.0,
    revalidate: bool = False,
) -> dict[str, int]:
    """CSVの画像URLを直列に取得して画像キャッシュを温める。取得数/スキップ数/失敗数を返す。

    キャッシュ済みURLはスキップ(待機もしない)。未キャッシュのみ download_image し、
    CSVの image_credit 列が空でクレジット未取得なら fetch_image_credit も行い、delay秒待つ。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = _collect_rows(csv_paths)
    total = len(rows)
    fetched = skipped = failed = revalidated = 0
    for i, (url, row) in enumerate(rows.items(), 1):
        was_cached = _image_cached(url, cache_dir)
        if was_cached and not revalidate:
            skipped += 1
            logger.info("[%d/%d] スキップ(キャッシュ済み): %s", i, total, url)
            continue
        action = "更新確認" if was_cached else "取得"
        logger.info("[%d/%d] %s: %s", i, total, action, url)
        raw = download_image(url, cache_dir, revalidate=revalidate)
        if raw is None:
            failed += 1
            continue
        if was_cached:
            revalidated += 1
        else:
            fetched += 1
        # image_credit 列が埋まっている行はクレジット取得不要(video側もその文言を優先)
        has_credit_col = bool(str(row.get("image_credit") or "").strip())
        if not has_credit_col and not _credit_cached(url, cache_dir):
            fetch_image_credit(url, (row.get("image_page") or "").strip(), cache_dir)
        if delay > 0:
            time.sleep(delay)
    logger.info(
        "prewarm完了: 取得 %d / 更新確認 %d / スキップ %d / 失敗 %d (URL計 %d)",
        fetched, revalidated, skipped, failed, total,
    )
    return {
        "fetched": fetched,
        "revalidated": revalidated,
        "skipped": skipped,
        "failed": failed,
        "total": total,
    }
