#!/usr/bin/env bash
set -euo pipefail

# Installed as a root-owned host controller. Never execute a copy from a checkout in
# response to an untrusted event; install an audited revision with install-auto-deploy.sh.
repo_url=https://github.com/soramimic/soramimic-video.git
deployer_user=soramimic-video-deployer
state_dir=/var/lib/soramimic-video-deployer
deploy_dir=/usr/local/libexec/soramimic-video-deploy
global_lock=/run/lock/soramimic-video-auto-deploy.lock
checks_url=https://api.github.com/repos/soramimic/soramimic-video/commits

if [[ ${SORAMIMIC_AUTO_DEPLOY_TEST:-0} == 1 ]]; then
  [[ $EUID -ne 0 ]] || { echo "test overrides are forbidden as root" >&2; exit 2; }
  repo_url=${SORAMIMIC_AUTO_DEPLOY_REPO_URL:?}
  deployer_user=$(id -un)
  state_dir=${SORAMIMIC_AUTO_DEPLOY_STATE_DIR:?}
  deploy_dir=${SORAMIMIC_AUTO_DEPLOY_LIBEXEC:?}
  global_lock=${SORAMIMIC_AUTO_DEPLOY_LOCK:?}
  checks_url=${SORAMIMIC_AUTO_DEPLOY_CHECKS_URL:?}
  test_root=${SORAMIMIC_AUTO_DEPLOY_TEST_ROOT:?}
elif [[ $EUID -ne 0 ]]; then
  echo "auto deploy controller must run as root" >&2
  exit 2
fi

mirror=$state_dir/repository.git
failed_dir=$state_dir/failed

branch_config() {
  case $1 in
    dev) app_root=/opt/soramimic-video-dev; port=8311 ;;
    preview) app_root=/opt/soramimic-video-preview; port=8312 ;;
    main) app_root=/opt/soramimic-video-public; port=8301 ;;
    *) echo "unsupported branch: $1" >&2; return 2 ;;
  esac
  if [[ ${SORAMIMIC_AUTO_DEPLOY_TEST:-0} == 1 ]]; then
    app_root="$test_root$app_root"
  fi
}

as_deployer() {
  if [[ ${SORAMIMIC_AUTO_DEPLOY_TEST:-0} == 1 ]]; then
    "$@"
  else
    runuser -u "$deployer_user" -- "$@"
  fi
}

init_mirror() {
  install -d -m 0750 -o "$deployer_user" -g "$deployer_user" "$state_dir" "$failed_dir"
  if [[ ! -d $mirror ]]; then
    as_deployer git init --bare --quiet "$mirror"
    as_deployer git --git-dir="$mirror" remote add origin "$repo_url"
  fi
  [[ $(as_deployer git --git-dir="$mirror" remote get-url origin) == "$repo_url" ]] || {
    echo "unexpected deploy mirror origin" >&2
    return 1
  }
}

fetch_head() {
  local branch=$1
  as_deployer git --git-dir="$mirror" fetch --quiet --force --no-tags origin \
    "+refs/heads/$branch:refs/remotes/origin/$branch" || return 75
  as_deployer git --git-dir="$mirror" rev-parse \
    "refs/remotes/origin/$branch^{commit}" || return 75
}

current_commit() {
  local metadata=$app_root/current/deploy-release.json
  [[ -f $metadata ]] || return 0
  jq -er '.commit' "$metadata"
}

assert_forward_update() {
  local current=$1 target=$2
  [[ -z $current || $current == "$target" ]] && return 0
  if ! as_deployer git --git-dir="$mirror" cat-file -e "$current^{commit}" 2>/dev/null; then
    as_deployer git --git-dir="$mirror" fetch --quiet --no-tags origin "$current" || return
  fi
  as_deployer git --git-dir="$mirror" merge-base --is-ancestor "$current" "$target" || {
    echo "refusing non-fast-forward deployment: $current -> $target" >&2
    return 1
  }
}

check_current() {
  curl -fsS --max-time 5 "http://127.0.0.1:$port/healthz" |
    jq -e '.status == "ok"' >/dev/null || return
  curl -fsS --max-time 5 "http://127.0.0.1:$port/readyz" |
    jq -e '.status == "ready" and (.checks | to_entries | all(.value == true))' >/dev/null || return
}

require_successful_ci() {
  local sha=$1 body
  body=$(curl -fsS --max-time 15 -H 'Accept: application/vnd.github+json' \
    "$checks_url/$sha/check-runs?per_page=100") || return 75
  if jq -e '[.check_runs[] | select(.name == "test" and .app.slug == "github-actions")] as $tests |
    (($tests | length) > 0 and ($tests | all(.status == "completed" and
      .conclusion == "success")))' <<<"$body" >/dev/null; then
    return 0
  fi
  if jq -e '[.check_runs[] | select(.name == "test" and .app.slug == "github-actions" and
    .status == "completed" and
    (.conclusion != "success" and .conclusion != "neutral" and
     .conclusion != "skipped"))] | length > 0' <<<"$body" >/dev/null; then
    echo "test check failed for $sha" >&2
    return 76
  fi
  echo "test check has not completed for $sha" >&2
  return 75
}

quarantine_path() {
  printf '%s/%s-%s' "$failed_dir" "$1" "$2"
}

deploy_branch() {
  local branch=$1 target current latest deployments
  branch_config "$branch"
  target=$(fetch_head "$branch") || return
  [[ $target =~ ^[0-9a-f]{40}$ ]] || { echo "invalid remote SHA: $target" >&2; return 1; }
  current=$(current_commit) || return
  if [[ $current == "$target" ]]; then
    check_current || return 77
    echo "$branch already deployed at $target"
    return 0
  fi
  if [[ -f $(quarantine_path "$branch" "$target") ]]; then
    echo "$branch $target is quarantined; advance the branch or clear the marker manually" >&2
    return 0
  fi
  require_successful_ci "$target" || return
  assert_forward_update "$current" "$target" || return 76
  deployments=$app_root/deployments
  if [[ ! -f $deployments/prepared-$target.json ]]; then
    "$deploy_dir/deploy-environment.sh" "$branch" prepare "$target" || return 76
  fi
  if [[ ! -f $deployments/verified-$target.json ]]; then
    "$deploy_dir/deploy-environment.sh" "$branch" verify "$target" || return 76
  fi

  # A newer merge may have arrived during the build. Never let an older run roll the
  # environment backward; the next timer invocation will build the new head.
  latest=$(fetch_head "$branch") || return
  if [[ $latest != "$target" ]]; then
    echo "$branch advanced during deployment ($target -> $latest); skipping activation"
    return 0
  fi
  require_successful_ci "$target" || return
  current=$(current_commit) || return
  if [[ $current == "$target" ]]; then
    check_current || return 77
    return 0
  fi
  assert_forward_update "$current" "$target" || return 76
  "$deploy_dir/deploy-environment.sh" "$branch" activate "$target" --confirm "$target" || return 76
  [[ $(current_commit) == "$target" ]] || return 76
  check_current || return 77
  echo "$branch deployed at $target"
}

main() {
  local failed=0 branch target='' rc
  command -v git >/dev/null
  command -v jq >/dev/null
  command -v curl >/dev/null
  exec 9>"$global_lock"
  flock -n 9 || { echo "another automatic deployment is running"; return 0; }
  init_mirror || return
  for branch in dev preview main; do
    rc=0
    deploy_branch "$branch" || rc=$?
    if (( rc != 0 )); then
      if (( rc == 75 )); then
        echo "automatic deployment is waiting for $branch CI" >&2
        continue
      fi
      echo "automatic deployment failed for $branch" >&2
      target=$(as_deployer git --git-dir="$mirror" rev-parse \
        "refs/remotes/origin/$branch^{commit}" 2>/dev/null || true)
      if (( rc == 76 )) && [[ $target =~ ^[0-9a-f]{40}$ ]]; then
        install -o "$deployer_user" -g "$deployer_user" -m 0640 /dev/null \
          "$(quarantine_path "$branch" "$target")"
      fi
      failed=1
    fi
  done
  return "$failed"
}

main "$@"
