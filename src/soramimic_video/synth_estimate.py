"""歌唱合成(NEUTRINO)の所要時間の見積り。

NEUTRINOがstdoutに進捗率を出す環境では実進捗をそのまま使えるが、
出力に進捗が無いバージョンや、実進捗が出るまでの序盤の見積りのために、
曲の長さ(秒)あたりの実処理秒数を実行のたびに指数移動平均で記録し、
次回の所要時間の目安に使う(係数をjobsディレクトリのJSONに保存する)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 実測(サンプル曲・4スレッド)で 音声43.2秒 に対し 実処理≒41.8秒 ≒ 0.97。
# 履歴が無いときの初期係数として使う(曲秒あたりの実処理秒)。
DEFAULT_FACTOR = 1.0
# 指数移動平均で新しい観測にかける重み(0〜1)。大きいほど直近を重視。
ALPHA = 0.3


def _load(store_path: Path) -> dict:
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_factor(store_path: Path) -> float:
    """記録済みの係数(曲秒あたりの実処理秒)。無ければ既定値。"""
    factor = _load(store_path).get("factor")
    if isinstance(factor, (int, float)) and factor > 0:
        return float(factor)
    return DEFAULT_FACTOR


def estimate_seconds(store_path: Path, score_seconds: float) -> float | None:
    """曲の長さ(秒)から合成の所要秒数を見積る。曲長が不明なら None。"""
    if score_seconds <= 0:
        return None
    return load_factor(store_path) * score_seconds


def record_run(
    store_path: Path,
    score_seconds: float,
    wall_seconds: float,
    alpha: float = ALPHA,
) -> None:
    """1回の合成実績(曲長と実処理秒)を指数移動平均で係数に反映する。"""
    if score_seconds <= 0 or wall_seconds <= 0:
        return
    observed = wall_seconds / score_seconds
    data = _load(store_path)
    old = data.get("factor")
    count = int(data.get("count", 0)) if isinstance(data.get("count"), int) else 0
    if isinstance(old, (int, float)) and old > 0 and count > 0:
        factor = (1 - alpha) * float(old) + alpha * observed
    else:
        factor = observed
    data["factor"] = round(factor, 4)
    data["count"] = count + 1
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:  # 記録は目安用途なので失敗しても合成は続行する
        logger.warning("合成所要時間の記録に失敗しました: %s", exc)
