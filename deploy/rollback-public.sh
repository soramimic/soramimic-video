#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "rootで実行してください" >&2
  exit 1
fi

: "${SORAMIMIC_ROLLBACK_USER:?旧user unitを動かすユーザーを指定してください}"

new_unit=${SORAMIMIC_NEW_UNIT:-soramimic-video-public.service}
old_unit=${SORAMIMIC_OLD_UNIT:-soramimic-video-public.service}
listen_port=${SORAMIMIC_LISTEN_PORT:-8301}
health_url=${SORAMIMIC_HEALTH_URL:-http://127.0.0.1:${listen_port}/healthz}
old_uid=$(id -u "$SORAMIMIC_ROLLBACK_USER")
old_runtime_dir="/run/user/$old_uid"

systemctl stop "$new_unit"

for _ in $(seq 1 30); do
  if ! ss -ltn "( sport = :${listen_port} )" | grep -q LISTEN; then
    break
  fi
  sleep 1
done
if ss -ltn "( sport = :${listen_port} )" | grep -q LISTEN; then
  echo "${listen_port}が解放されません" >&2
  exit 1
fi

runuser -u "$SORAMIMIC_ROLLBACK_USER" -- env XDG_RUNTIME_DIR="$old_runtime_dir" \
  systemctl --user start "$old_unit"

for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
    echo "rollback health: ok"
    exit 0
  fi
  sleep 1
done

echo "旧serviceは起動しましたがhealth確認に失敗しました" >&2
exit 1
