#!/usr/bin/env bash
# 13曲 × 4単語リスト × 3経路(Python / Node+MeCab注入 / Node+kuromoji)を通す。
# 計測を歪めないよう直列実行する。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-/home/jiro/development/soramimic-video/.venv/bin/python}"
SONGS="akatombo chatsumi furusato harugakita katatsumuri momiji momotarou nanatsunoko oborodukiyo shabondama lemon ussewa yorunikakeru"
LISTS="${LISTS:-nations plant scientist stations}"

cd "$HERE/.."
for wl in $LISTS; do
  echo "===== $wl / python"
  $PY spike/py/run_python.py "$wl" $SONGS
  echo "===== $wl / node injected(MeCab)"
  $PY spike/py/run_node.py "$wl" injected $SONGS
  echo "===== $wl / node kuromoji"
  $PY spike/py/run_node.py "$wl" kuromoji $SONGS
done
echo "done"
