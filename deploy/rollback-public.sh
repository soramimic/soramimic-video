#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/public-deploy-lib.sh
source "$script_dir/public-deploy-lib.sh"

app_root=${SORAMIMIC_APP_ROOT:-/opt/soramimic-video-public}
releases_dir=${SORAMIMIC_RELEASES_DIR:-$app_root/releases}
deployments_dir=${SORAMIMIC_DEPLOYMENTS_DIR:-$app_root/deployments}
current_link=${SORAMIMIC_CURRENT_LINK:-$app_root/current}
service_unit=${SORAMIMIC_SERVICE_UNIT:-soramimic-video-public.service}
listen_port=${SORAMIMIC_LISTEN_PORT:-8301}
health_attempts=${SORAMIMIC_HEALTH_ATTEMPTS:-45}

usage() {
  cat <<'EOF'
Usage:
  sudo deploy/rollback-public.sh release [--to <40-character-SHA>] --confirm <target-SHA>
  sudo SORAMIMIC_ROLLBACK_USER=<user> deploy/rollback-public.sh legacy-user-unit

Without --to, release rollback uses previous_release in the newest activation record for
the current release. The exact target SHA must always be repeated with --confirm.
EOF
}

legacy_user_unit() {
  local rollback_user=${SORAMIMIC_ROLLBACK_USER:?旧user unitのユーザーを指定してください}
  local old_unit=${SORAMIMIC_OLD_UNIT:-soramimic-video-public.service}
  local old_uid old_runtime_dir
  old_uid=$(id -u "$rollback_user")
  old_runtime_dir="/run/user/$old_uid"
  [[ $(runuser -u "$rollback_user" -- env XDG_RUNTIME_DIR="$old_runtime_dir" \
    systemctl --user show "$old_unit" --property=LoadState --value 2>/dev/null || true) == loaded ]] || \
    die "旧user manager/unitを確認できないためsystem unitは停止しません"
  systemctl stop "$service_unit"
  for _ in $(seq 1 30); do
    if ! ss -ltn "( sport = :${listen_port} )" | grep -q LISTEN; then break; fi
    sleep 1
  done
  if ss -ltn "( sport = :${listen_port} )" | grep -q LISTEN; then
    die "${listen_port}が解放されません"
  fi
  if runuser -u "$rollback_user" -- env XDG_RUNTIME_DIR="$old_runtime_dir" \
    systemctl --user start "$old_unit" && \
    wait_json_status "http://127.0.0.1:$listen_port/healthz" ok 30; then
    log "legacy user unit rollback health: ok"
    return
  fi
  echo "旧user unitの復帰に失敗したためsystem unitを再起動します" >&2
  runuser -u "$rollback_user" -- env XDG_RUNTIME_DIR="$old_runtime_dir" \
    systemctl --user stop "$old_unit" >/dev/null 2>&1 || true
  if systemctl restart "$service_unit" && \
    wait_json_status "http://127.0.0.1:$listen_port/healthz" ok "$health_attempts" && \
    wait_json_status "http://127.0.0.1:$listen_port/readyz" ready 5; then
    die "legacy rollback failed; system unitを復元しhealth/readinessを確認しました"
  fi
  die "legacy rollback failed; system unitのhealth/readinessも確認できません"
}

release_rollback() {
  local target_sha='' confirmed='' target current latest record restored=0
  while (( $# )); do
    case $1 in
      --to) target_sha=${2:-}; shift 2 ;;
      --confirm) confirmed=${2:-}; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  current=$(current_release)
  [[ -n $current ]] || die "current releaseがありません"
  if [[ -z $target_sha ]]; then
    latest=$(find "$deployments_dir" -maxdepth 1 -type f -name 'activated-*.json' -print | sort | tail -1)
    [[ -n $latest ]] || die "activation記録がありません。--toを指定してください"
    [[ $(jq -er .release_dir "$latest") == "$current" ]] || \
      die "最新activation記録とcurrentが一致しません。--toを指定してください"
    target=$(jq -er '.previous_release | select(length > 0)' "$latest") || \
      die "previous releaseが記録されていません"
    target_sha=$(jq -er '.previous_commit | select(length == 40)' "$latest" 2>/dev/null || true)
    if [[ -z $target_sha && -f $target/deploy-release.json ]]; then
      target_sha=$(jq -er .commit "$target/deploy-release.json")
    fi
    [[ -n $target_sha ]] || die "previous commitが記録されていません。--toを指定してください"
  else
    require_sha "$target_sha"
    target=$(release_for_sha "$target_sha")
  fi
  require_sha "$target_sha"
  [[ $confirmed == "$target_sha" ]] || \
    die "rollbackにはtarget SHAの再入力が必要です: --confirm $target_sha"
  assert_release "$target" "$target_sha"
  [[ $target != "$current" ]] || die "targetは既にcurrentです"
  atomic_link "$target" "$current_link"
  if systemctl restart "$service_unit" && \
    wait_json_status "http://127.0.0.1:$listen_port/healthz" ok "$health_attempts" && \
    wait_json_status "http://127.0.0.1:$listen_port/readyz" ready 5; then
    restored=1
  fi
  if (( ! restored )); then
    echo "rollback先の確認に失敗したため元のreleaseへ戻します" >&2
    atomic_link "$current" "$current_link"
    if systemctl restart "$service_unit" && \
      wait_json_status "http://127.0.0.1:$listen_port/healthz" ok "$health_attempts" && \
      wait_json_status "http://127.0.0.1:$listen_port/readyz" ready 5; then
      die "rollback failed; original releaseを復元しhealth/readinessを確認しました"
    fi
    die "rollback failed; originalへ切り替えましたがhealth/readinessを確認できません"
  fi
  record="$deployments_dir/rollback-$(date -u +%Y%m%dT%H%M%SZ)-$$-$target_sha.json"
  [[ ! -e $record ]] || die "rollback recordが既に存在します"
  jq -n --arg commit "$target_sha" --arg release_dir "$target" --arg previous "$current" \
    --arg rolled_back_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schema:1,commit:$commit,release_dir:$release_dir,previous_release:$previous,
      rolled_back_at:$rolled_back_at,service_health:"ok",service_readiness:"ready"}' >"$record"
  chmod 0600 "$record"
  log "rolled back: $target"
}

require_root
acquire_deploy_lock
case ${1:-} in
  release) shift; release_rollback "$@" ;;
  legacy-user-unit) shift; [[ $# -eq 0 ]] || die "追加引数はありません"; legacy_user_unit ;;
  *) usage >&2; exit 2 ;;
esac
