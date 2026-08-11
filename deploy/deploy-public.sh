#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/public-deploy-lib.sh
source "$script_dir/public-deploy-lib.sh"

app_root=${SORAMIMIC_APP_ROOT:-/opt/soramimic-video-public}
releases_dir=${SORAMIMIC_RELEASES_DIR:-$app_root/releases}
deployments_dir=${SORAMIMIC_DEPLOYMENTS_DIR:-$app_root/deployments}
current_link=${SORAMIMIC_CURRENT_LINK:-$app_root/current}
state_root=${SORAMIMIC_STATE_ROOT:-/var/lib/soramimic-video-public/work}
env_file=${SORAMIMIC_ENV_FILE:-/etc/soramimic-video/public.env}
service_user=${SORAMIMIC_SERVICE_USER:-soramimic-video}
service_group=${SORAMIMIC_SERVICE_GROUP:-soramimic-video}
service_unit=${SORAMIMIC_SERVICE_UNIT:-soramimic-video-public.service}
repo_url=${SORAMIMIC_REPO_URL:-https://github.com/soramimic/soramimic-video.git}
source_checkout=${SORAMIMIC_SOURCE_CHECKOUT:-}
source_ref=${SORAMIMIC_SOURCE_REF:-main}
uv_bin=${SORAMIMIC_UV_BIN:-}
staging_port=${SORAMIMIC_STAGING_PORT:-18301}
production_port=${SORAMIMIC_LISTEN_PORT:-8301}
health_attempts=${SORAMIMIC_HEALTH_ATTEMPTS:-45}
proc_root=${SORAMIMIC_PROC_ROOT:-/proc}
dry_run=0

usage() {
  cat <<'EOF'
Usage:
  sudo deploy/deploy-public.sh [--dry-run] prepare  <40-character-commit-SHA> [--source <checkout>]
  sudo deploy/deploy-public.sh [--dry-run] verify   <40-character-commit-SHA>
  sudo deploy/deploy-public.sh [--dry-run] activate <40-character-commit-SHA> --confirm <same-SHA>
    [--previous-sha <SHA, first migration only>]

prepare builds an immutable release but does not start it. verify starts it only on the
localhost staging port and records successful checks. activate requires that record and
an exact SHA confirmation; it atomically switches current and restarts production.
EOF
}

if [[ ${1:-} == --dry-run ]]; then
  dry_run=1
  shift
fi
command=${1:-}
sha=${2:-}
[[ -n $command && -n $sha ]] || { usage >&2; exit 2; }
shift 2
require_sha "$sha"

run() {
  if (( dry_run )); then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

ensure_layout() {
  run install -d -m 0755 "$app_root" "$releases_dir"
  run install -d -m 0700 "$deployments_dir"
}

# Export only tracked files, including nested submodules, from a credentialed local
# checkout. This avoids giving the service account GitHub credentials.
archive_tree() {
  local source=$1 destination=$2 expected=$3 actual mode object path
  actual=$(git -C "$source" rev-parse HEAD)
  [[ $actual == "$expected" ]] || die "checkout SHA mismatch: $source ($actual != $expected)"
  [[ -z $(git -C "$source" status --porcelain --untracked-files=no) ]] || \
    die "tracked変更があるcheckoutはsourceにできません: $source"
  git -C "$source" archive "$expected" | tar -x -C "$destination"
  while read -r mode object _ path; do
    [[ $mode == 160000 ]] || continue
    [[ -d $source/$path ]] || die "submoduleがcheckoutされていません: $source/$path"
    install -d -m 0750 "$destination/$path"
    archive_tree "$source/$path" "$destination/$path" "$object"
  done < <(git -C "$source" ls-files --stage)
}

prepare() {
  local timestamp short release_name release_dir build_home resolved submodules manifest prepared_record
  command -v git >/dev/null || die "gitが必要です"
  git check-ref-format --branch "$source_ref" >/dev/null 2>&1 || die "不正なsource branchです: $source_ref"
  if [[ -z $uv_bin ]]; then
    uv_bin=$(command -v uv || true)
  fi
  [[ $uv_bin == /* ]] || die "uvの絶対pathをSORAMIMIC_UV_BINで指定してください"
  [[ -x $uv_bin ]] || die "uvを実行できません: $uv_bin"
  command -v npm >/dev/null || die "npmが必要です"
  command -v jq >/dev/null || die "jqが必要です"
  id "$service_user" >/dev/null 2>&1 || die "service userがありません: $service_user"
  getent group "$service_group" >/dev/null || die "service groupがありません: $service_group"
  runuser -u "$service_user" -- test -x "$uv_bin" || \
    die "service userがuvを実行できません。/usr/local/bin等へroot所有で配置してください: $uv_bin"
  ensure_layout
  [[ ! -e $deployments_dir/prepared-$sha.json ]] || die "このSHAのprepare記録は既にあります"
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  short=${sha:0:12}
  release_name="$timestamp-$short"
  release_dir="$releases_dir/$release_name"
  build_home="$release_dir/.build-home"
  manifest="$deployments_dir/manifest-$sha.sha256"
  prepared_record="$deployments_dir/prepared-$sha.json"
  [[ ! -e $release_dir ]] || die "release pathが既に存在します"
  [[ ! -e $manifest ]] || die "manifestが既にあります: $manifest"
  if [[ -n $source_checkout ]]; then
    source_checkout=$(realpath "$source_checkout")
    [[ -d $source_checkout ]] || die "source checkoutがありません: $source_checkout"
    resolved=$(git -C "$source_checkout" rev-parse HEAD)
    [[ $resolved == "$sha" ]] || die "source checkoutのHEADが指定SHAではありません"
    git -C "$source_checkout" merge-base --is-ancestor "$sha" "refs/remotes/origin/$source_ref" || \
      die "指定SHAはsource checkoutのorigin/$source_refに含まれません（先にgit fetchしてください）"
  fi
  if (( dry_run )); then
    log "$source_ref SHAを検証してreleaseを構築: $release_dir"
    if [[ -n $source_checkout ]]; then
      log "would archive local source: $source_checkout"
    else
      run git clone --no-checkout "$repo_url" "$release_dir"
    fi
    run "$uv_bin" sync --frozen --extra api --no-dev
    run scripts/build-editor.sh
    return
  fi

  # venv console scripts contain absolute shebangs. Build at the final path and
  # delete it on every failure; current is only touched by the separate activate command.
  trap 'chmod -R u+w "$release_dir" >/dev/null 2>&1 || true; rm -rf -- "$release_dir"; rm -f -- "$manifest" "$prepared_record"' EXIT
  install -d -m 0750 -o "$service_user" -g "$service_group" "$release_dir"
  install -d -m 0700 -o "$service_user" -g "$service_group" "$build_home"
  if [[ -n $source_checkout ]]; then
    archive_tree "$source_checkout" "$release_dir" "$sha"
    chown -R "$service_user":"$service_group" "$release_dir"
    submodules=$(git -C "$source_checkout" submodule status --recursive 2>/dev/null || true)
  else
    runuser -u "$service_user" -- env HOME="$build_home" \
      git clone --no-checkout --filter=blob:none "$repo_url" "$release_dir/repo"
    mv "$release_dir/repo"/.git "$release_dir/.git"
    shopt -s dotglob nullglob
    mv "$release_dir/repo"/* "$release_dir/"
    shopt -u dotglob nullglob
    rmdir "$release_dir/repo"
    runuser -u "$service_user" -- env HOME="$build_home" \
      git -C "$release_dir" fetch --no-tags origin "$source_ref"
    resolved=$(runuser -u "$service_user" -- git -C "$release_dir" rev-parse "$sha^{commit}") || \
      die "remoteでSHAを取得できません"
    [[ $resolved == "$sha" ]] || die "commit SHAを解決できません"
    runuser -u "$service_user" -- git -C "$release_dir" merge-base --is-ancestor "$sha" FETCH_HEAD || \
      die "指定SHAはorigin/$source_refに含まれません"
    runuser -u "$service_user" -- env HOME="$build_home" git -C "$release_dir" checkout --detach "$sha"
    runuser -u "$service_user" -- env HOME="$build_home" \
      git -C "$release_dir" submodule update --init --recursive --depth 1
    submodules=$(git -C "$release_dir" submodule status --recursive)
  fi
  runuser -u "$service_user" -- env HOME="$build_home" UV_PROJECT_ENVIRONMENT="$release_dir/.venv" \
    "$uv_bin" sync --directory "$release_dir" --frozen --extra api --no-dev
  runuser -u "$service_user" -- env HOME="$build_home" "$release_dir/scripts/build-editor.sh"
  assert_final_venv_shebang "$release_dir"
  rm -rf -- "$build_home"
  jq -n \
    --arg commit "$sha" --arg source_ref "$source_ref" --arg repository "$repo_url" \
    --arg built_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg release_dir "$release_dir" \
    --arg submodules "$submodules" \
    '{schema:1,commit:$commit,source_ref:$source_ref,repository:$repository,
      built_at:$built_at,release_dir:$release_dir,submodules:($submodules|split("\n"))}' \
    >"$release_dir/deploy-release.json"
  chown -R root:"$service_group" "$release_dir"
  chmod -R u=rwX,g=rX,o= "$release_dir"
  (cd "$release_dir" && find . -type f -print0 | sort -z | xargs -0 sha256sum) >"$manifest"
  chmod -R a-w "$release_dir"
  jq -n --arg commit "$sha" --arg release_dir "$release_dir" --arg manifest "$manifest" \
    --arg manifest_sha256 "$(sha256sum "$manifest" | cut -d' ' -f1)" \
    --arg symlink_sha256 "$(symlink_digest "$release_dir")" \
    --arg prepared_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schema:1,commit:$commit,release_dir:$release_dir,prepared_at:$prepared_at,
      manifest:$manifest,manifest_sha256:$manifest_sha256,symlink_sha256:$symlink_sha256}' \
    >"$prepared_record"
  chmod 0600 "$prepared_record"
  trap - EXIT
  log "prepared: $release_dir"
}

verify() {
  local release_dir unit staging_jobs health_url ready_url main_pid active_state pid_cwd listener_ok
  command -v systemd-run >/dev/null || die "systemd-runが必要です"
  release_dir=$(release_for_sha "$sha")
  assert_release "$release_dir" "$sha"
  [[ ! -e $deployments_dir/verified-$sha.json ]] || die "このSHAのverify記録は既にあります"
  [[ -r $env_file ]] || die "environment fileが読めません: $env_file"
  install -d -m 0700 -o "$service_user" -g "$service_group" "$state_root"
  staging_jobs="$state_root/deploy-staging-$sha"
  install -d -m 0700 -o "$service_user" -g "$service_group" "$staging_jobs"
  unit="soramimic-video-staging-${sha:0:12}-$$.service"
  health_url="http://127.0.0.1:$staging_port/healthz"
  ready_url="http://127.0.0.1:$staging_port/readyz"
  if ss -ltn "( sport = :${staging_port} )" | grep -q LISTEN; then
    die "staging portが既に使用中です: $staging_port"
  fi
  if (( dry_run )); then
    run systemd-run --unit "$unit" --property "User=$service_user" \
      "$release_dir/.venv/bin/soramimic-video" serve --host 127.0.0.1 --port "$staging_port"
    log "would check $health_url, $ready_url, /, /api/config, /editor/editor.html"
    return
  fi
  systemctl reset-failed "$unit" >/dev/null 2>&1 || true
  trap 'systemctl stop "$unit" >/dev/null 2>&1 || true; rm -rf -- "$staging_jobs"' EXIT
  systemd-run --quiet --unit "$unit" --collect \
    --property=Type=simple --property="User=$service_user" --property="Group=$service_group" \
    --property="WorkingDirectory=$release_dir" --property="EnvironmentFile=$env_file" \
    --setenv="HOME=$state_root" --setenv=SORAMIMIC_ALLOW_LOCAL_OPS=1 \
    "$release_dir/.venv/bin/soramimic-video" serve --host 127.0.0.1 \
    --port "$staging_port" --jobs-dir "$staging_jobs" --voicevox-url http://127.0.0.1:50021
  active_state=''
  main_pid=0
  for _ in $(seq 1 10); do
    active_state=$(systemctl show "$unit" --property=ActiveState --value 2>/dev/null || true)
    main_pid=$(systemctl show "$unit" --property=MainPID --value 2>/dev/null || true)
    if [[ $active_state == active && $main_pid =~ ^[1-9][0-9]*$ ]]; then break; fi
    sleep 1
  done
  [[ $active_state == active && $main_pid =~ ^[1-9][0-9]*$ ]] || \
    die "staging transient unitがactiveになりません"
  pid_cwd=$(readlink -f "$proc_root/$main_pid/cwd" 2>/dev/null || true)
  [[ $pid_cwd == "$release_dir" ]] || die "staging MainPIDのrelease identityが一致しません"
  listener_ok=0
  for _ in $(seq 1 10); do
    if [[ $(systemctl show "$unit" --property=ActiveState --value 2>/dev/null) == active ]] && \
      [[ $(systemctl show "$unit" --property=MainPID --value 2>/dev/null) == "$main_pid" ]] && \
      ss -ltnp "( sport = :${staging_port} )" | grep -q "pid=$main_pid,"; then
      listener_ok=1
      break
    fi
    sleep 1
  done
  (( listener_ok )) || die "staging portのlistenerがtransient unit MainPIDではありません"
  wait_json_status "$health_url" ok "$health_attempts" || die "staging health確認に失敗しました"
  wait_json_status "$ready_url" ready 5 || die "staging readiness確認に失敗しました"
  curl -fsS --max-time 5 "$health_url" | jq -e '.status == "ok"' >/dev/null
  curl -fsS --max-time 5 "$ready_url" | jq -e \
    '.status == "ready" and (.checks | to_entries | all(.value == true))' >/dev/null
  smoke_public_surface "http://127.0.0.1:$staging_port" || die "staging surface smokeに失敗しました"
  systemctl stop "$unit"
  rm -rf -- "$staging_jobs"
  trap - EXIT
  jq -n --arg commit "$sha" --arg release_dir "$release_dir" \
    --arg manifest_sha256 "$(jq -er .manifest_sha256 "$deployments_dir/prepared-$sha.json")" \
    --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{schema:1,commit:$commit,release_dir:$release_dir,manifest_sha256:$manifest_sha256,verified_at:$verified_at,
      checks:["healthz","readyz","index","api-config","editor"]}' \
    >"$deployments_dir/verified-$sha.json"
  chmod 0600 "$deployments_dir/verified-$sha.json"
  log "staging verified: $release_dir"
}

activate() {
  local release_dir verified confirmed='' previous='' previous_sha='' activated_at record failed=0
  while (( $# )); do
    case $1 in
      --confirm) confirmed=${2:-}; shift 2 ;;
      --previous-sha) previous_sha=${2:-}; shift 2 ;;
      *) die "unknown activate option: $1" ;;
    esac
  done
  [[ $confirmed == "$sha" ]] || die "本番切替には --confirm $sha が必要です"
  release_dir=$(release_for_sha "$sha")
  assert_release "$release_dir" "$sha"
  verified="$deployments_dir/verified-$sha.json"
  [[ -f $verified ]] || die "verify成功記録がありません: $verified"
  [[ $(jq -er .release_dir "$verified") == "$release_dir" ]] || die "verify対象releaseが一致しません"
  [[ $(jq -er .manifest_sha256 "$verified") == \
    "$(jq -er .manifest_sha256 "$deployments_dir/prepared-$sha.json")" ]] || \
    die "verify後にrelease manifest identityが変わっています"
  systemctl show "$service_unit" --property=Environment --value | \
    tr ' ' '\n' | grep -qx 'SORAMIMIC_ALLOW_LOCAL_OPS=1' || \
    die "installed unitにSORAMIMIC_ALLOW_LOCAL_OPS=1がありません。unit配置とdaemon-reloadを先に実行してください"
  previous=$(current_release || true)
  if [[ -n $previous ]]; then
    if [[ -f $previous/deploy-release.json ]]; then
      previous_sha=$(jq -er .commit "$previous/deploy-release.json")
    elif [[ -n $previous_sha ]]; then
      require_sha "$previous_sha"
      [[ $previous_sha != "$sha" ]] || die "previous SHAはnew SHAと同じにできません"
      [[ $previous == "$releases_dir/"* && -x $previous/.venv/bin/soramimic-video ]] || \
        die "bootstrap previous releaseを検証できません"
      [[ ${previous##*/} == *"-${previous_sha:0:12}" ]] || \
        die "bootstrap release名のshort SHAが--previous-shaと一致しません"
      local detected_previous=''
      if [[ -e $previous/.git ]]; then
        detected_previous=$(git -c safe.directory="$previous" -C "$previous" rev-parse HEAD 2>/dev/null || true)
      elif [[ -f $previous/REVISION ]]; then
        detected_previous=$(tr -d '[:space:]' <"$previous/REVISION")
      fi
      if [[ -n $detected_previous && $detected_previous != "$previous_sha" ]]; then
        die "--previous-shaが旧releaseの記録済みSHAと一致しません"
      fi
      if (( ! dry_run )); then
        [[ ! -e $deployments_dir/prepared-$previous_sha.json ]] || \
          die "bootstrap prepared recordが既に存在します"
        jq -n --arg commit "$previous_sha" --arg release_dir "$previous" \
          --arg recorded_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          '{schema:1,commit:$commit,release_dir:$release_dir,recorded_at:$recorded_at,bootstrap:true}' \
          >"$deployments_dir/prepared-$previous_sha.json"
        chmod 0600 "$deployments_dir/prepared-$previous_sha.json"
      fi
    else
      die "既存currentにmetadataがありません。初回だけ --previous-sha <現在版の40桁SHA> が必要です"
    fi
  elif [[ -n $previous_sha ]]; then
    die "currentがないため --previous-sha は指定できません"
  fi
  if (( dry_run )); then
    log "would activate $release_dir (previous: ${previous:-none})"
    run systemctl restart "$service_unit"
    return
  fi
  atomic_link "$release_dir" "$current_link"
  if ! systemctl restart "$service_unit" || \
    ! wait_json_status "http://127.0.0.1:$production_port/healthz" ok "$health_attempts" || \
    ! wait_json_status "http://127.0.0.1:$production_port/readyz" ready 5 || \
    ! smoke_public_surface "http://127.0.0.1:$production_port"; then
    failed=1
  fi
  if (( failed )); then
    echo "本番確認に失敗したためpreviousへ復帰します" >&2
    if [[ -n $previous && -d $previous ]]; then
      atomic_link "$previous" "$current_link"
      if systemctl restart "$service_unit" && \
        wait_json_status "http://127.0.0.1:$production_port/healthz" ok "$health_attempts" && \
        wait_json_status "http://127.0.0.1:$production_port/readyz" ready 5; then
        die "activation failed; previousを復元しhealth/readinessを確認しました"
      fi
      die "activation failed; previousへ切り替えましたがhealth/readinessを確認できません"
    else
      rm -f -- "$current_link"
      systemctl stop "$service_unit" || true
    fi
    die "activation failed; previousがないためserviceを停止しました"
  fi
  activated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  record="$deployments_dir/activated-$(date -u +%Y%m%dT%H%M%SZ)-$$-$sha.json"
  [[ ! -e $record ]] || die "activation recordが既に存在します"
  jq -n --arg commit "$sha" --arg release_dir "$release_dir" --arg previous "$previous" \
    --arg previous_commit "$previous_sha" \
    --arg activated_at "$activated_at" \
    '{schema:1,commit:$commit,release_dir:$release_dir,previous_release:$previous,
      previous_commit:$previous_commit,activated_at:$activated_at,
      service_health:"ok",service_readiness:"ready"}' >"$record"
  chmod 0600 "$record"
  log "activated: $release_dir"
}

require_root
acquire_deploy_lock
case $command in
  prepare)
    if (( $# )); then
      [[ $1 == --source && $# -eq 2 ]] || die "prepare optionは --source <checkout> です"
      source_checkout=$2
    fi
    prepare
    ;;
  verify) [[ $# -eq 0 ]] || die "verifyに追加引数はありません"; verify ;;
  activate) activate "$@" ;;
  *) usage >&2; exit 2 ;;
esac
