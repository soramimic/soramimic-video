#!/usr/bin/env bash
# soramimic editor(submodule)を /editor/ 配下で配信できる静的ファイルにビルドする。
#
# 生成物は external/soramimic/frontend/dist/ に出力され、APIサーバー(serve)が
# /editor/ にマウントして配信する(A-2: editorをWebUIに同梱)。base=/editor/ を
# 付けてビルドしないと、アセットのURLがルート絶対パスになり /editor/ 配下で
# 404 になるので注意。kuromoji辞書(.dat.gz)は prebuild スクリプトで
# node_modules から public/kuromoji/dict へ同期され、dist にも入る。
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "$0")/../external/soramimic/frontend" && pwd)"
cd "$FRONTEND_DIR"

echo "==> npm ci ($FRONTEND_DIR)"
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi

echo "==> sync-dict (kuromoji辞書を public/kuromoji/dict へ)"
# npm run build なら prebuild フックで同期されるが、直接 vite build する場合は
# フックが走らないので明示的に同期する。これが無いと dist に辞書が入らず
# editor の形態素解析(再生成)が動かない。
npm run sync-dict

echo "==> vite build --base=/editor/"
npx vite build --base=/editor/

echo "==> ビルド完了: $FRONTEND_DIR/dist"
