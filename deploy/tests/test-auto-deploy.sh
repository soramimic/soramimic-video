#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
controller="$repo_root/deploy/auto-deploy-branches.sh"
automerge_workflow="$repo_root/.github/workflows/automerge.yaml"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

# GITHUB_TOKEN merges suppress push workflows, so automerge must publish the
# already-verified PR result as an exact merge-SHA check for the host controller.
grep -Fx '  checks: write' "$automerge_workflow" >/dev/null
grep -F 'merge_sha=$(echo "$merge" | jq -r .sha)' "$automerge_workflow" >/dev/null
grep -F 'gh api -X POST "repos/$REPO/check-runs"' "$automerge_workflow" >/dev/null
grep -F -- '-f name=test -f head_sha="$merge_sha" -f status=completed' \
  "$automerge_workflow" >/dev/null
grep -F 'and .app.slug == "github-actions"' "$automerge_workflow" >/dev/null
grep -F 'and .conclusion != "success")] | length' "$automerge_workflow" >/dev/null
grep -F '"$test_total" -gt 0' "$automerge_workflow" >/dev/null
grep -F 'if [ "$tested_shape" != "$actual_shape" ]' "$automerge_workflow" >/dev/null

automerge_gate() {
  local runs=$1 total incomplete failed tests test_total test_incomplete test_unsuccessful
  total=$(jq 'length' <<<"$runs")
  incomplete=$(jq '[.[] | select(.status != "completed")] | length' <<<"$runs")
  failed=$(jq '[.[] | select(.status == "completed" and .conclusion != "success" and
    .conclusion != "neutral" and .conclusion != "skipped")] | length' <<<"$runs")
  tests=$(jq '[.[] | select(.name == "test" and .app.slug == "github-actions")]' <<<"$runs")
  test_total=$(jq 'length' <<<"$tests")
  test_incomplete=$(jq '[.[] | select(.status != "completed")] | length' <<<"$tests")
  test_unsuccessful=$(jq '[.[] | select(.status == "completed" and
    .conclusion != "success")] | length' <<<"$tests")
  (( total > 0 && incomplete == 0 && failed == 0 && test_total > 0 &&
    test_incomplete == 0 && test_unsuccessful == 0 ))
}
success_test='{"name":"test","app":{"slug":"github-actions"},"status":"completed","conclusion":"success"}'
skipped_test='{"name":"test","app":{"slug":"github-actions"},"status":"completed","conclusion":"skipped"}'
irrelevant='{"name":"Dependency review","app":{"slug":"github-actions"},"status":"completed","conclusion":"skipped"}'
failed_other='{"name":"lint","app":{"slug":"github-actions"},"status":"completed","conclusion":"failure"}'
! automerge_gate "[$irrelevant]"
! automerge_gate "[$skipped_test]"
automerge_gate "[$success_test,$irrelevant]"
! automerge_gate "[$success_test,$failed_other]"

git init --bare --quiet "$tmp/remote.git"
git init --quiet "$tmp/source"
git -C "$tmp/source" config user.email test@example.invalid
git -C "$tmp/source" config user.name test
touch "$tmp/source/file"
git -C "$tmp/source" add file
git -C "$tmp/source" commit --quiet -m initial
for branch in dev preview main; do
  git -C "$tmp/source" branch "$branch"
  git -C "$tmp/source" push --quiet "$tmp/remote.git" "$branch"
done
sha=$(git -C "$tmp/source" rev-parse HEAD)

mkdir -p "$tmp/libexec" "$tmp/bin"
mkdir -p "$tmp/checks"
real_git=$(command -v git)
cat >"$tmp/libexec/deploy-environment.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
branch=$1 stage=$2 sha=$3
case $branch in
  dev) root=$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/opt/soramimic-video-dev ;;
  preview) root=$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/opt/soramimic-video-preview ;;
  main) root=$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/opt/soramimic-video-public ;;
  *) exit 2 ;;
esac
mkdir -p "$root/deployments" "$root/releases/$sha"
printf '%s %s %s\n' "$branch" "$stage" "$sha" >>"$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/calls"
case $stage in
  prepare)
    printf '{"release_dir":"%s/releases/%s"}\n' "$root" "$sha" >"$root/deployments/prepared-$sha.json"
    if [[ ${SORAMIMIC_AUTO_DEPLOY_TEST_ADVANCE:-} == "$branch" && \
          ! -e $SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/advanced ]]; then
      touch "$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/advanced"
      echo advanced >>"$SORAMIMIC_AUTO_DEPLOY_TEST_SOURCE/file"
      git -C "$SORAMIMIC_AUTO_DEPLOY_TEST_SOURCE" add file
      git -C "$SORAMIMIC_AUTO_DEPLOY_TEST_SOURCE" commit --quiet -m advanced
      git -C "$SORAMIMIC_AUTO_DEPLOY_TEST_SOURCE" push --quiet \
        "$SORAMIMIC_AUTO_DEPLOY_REPO_URL" HEAD:"$branch"
    fi
    ;;
  verify) printf '{}\n' >"$root/deployments/verified-$sha.json" ;;
  activate)
    [[ ${SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_ACTIVATE:-} != "$branch" ]] || exit 1
    printf '{"commit":"%s"}\n' "$sha" >"$root/releases/$sha/deploy-release.json"
    ln -sfn "$root/releases/$sha" "$root/current"
    ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$tmp/libexec/deploy-environment.sh"
cat >"$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
case ${*: -1} in
  *check-runs*)
    sha=${*: -1}; sha=${sha%/check-runs*}; sha=${sha##*/}
    cat "$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/checks/$sha.json"
    ;;
  */healthz) printf '{"status":"ok"}\n' ;;
  */readyz) printf '{"status":"ready","checks":{"service":true}}\n' ;;
  *) exit 22 ;;
esac
EOF
chmod +x "$tmp/bin/curl"
cat >"$tmp/bin/git" <<'EOF'
#!/usr/bin/env bash
if [[ ${SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_FETCH_ONCE:-0} == 1 && \
      $* == *'fetch --quiet --force'* && ! -e $SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/fetch-failed ]]; then
  touch "$SORAMIMIC_AUTO_DEPLOY_TEST_ROOT/fetch-failed"
  exit 1
fi
exec "$SORAMIMIC_AUTO_DEPLOY_TEST_REAL_GIT" "$@"
EOF
chmod +x "$tmp/bin/git"

run_controller() {
  env PATH="$tmp/bin:$PATH" SORAMIMIC_AUTO_DEPLOY_TEST=1 \
    SORAMIMIC_AUTO_DEPLOY_REPO_URL="$tmp/remote.git" \
    SORAMIMIC_AUTO_DEPLOY_STATE_DIR="$tmp/state" \
    SORAMIMIC_AUTO_DEPLOY_LIBEXEC="$tmp/libexec" \
    SORAMIMIC_AUTO_DEPLOY_LOCK="$tmp/deploy.lock" \
    SORAMIMIC_AUTO_DEPLOY_CHECKS_URL=http://checks \
    SORAMIMIC_AUTO_DEPLOY_TEST_SOURCE="$tmp/source" \
    SORAMIMIC_AUTO_DEPLOY_TEST_REAL_GIT="$real_git" \
    SORAMIMIC_AUTO_DEPLOY_TEST_ROOT="$tmp" "$controller"
}

mark_ci_success() {
  printf '{"check_runs":[{"name":"test","app":{"slug":"github-actions"},"status":"completed","conclusion":"success"}]}\n' \
    >"$tmp/checks/$1.json"
}

mark_ci_success "$sha"
run_controller >/dev/null 2>&1
[[ $(wc -l <"$tmp/calls") -eq 9 ]]
for branch in dev preview main; do
  grep -qx "$branch prepare $sha" "$tmp/calls"
  grep -qx "$branch verify $sha" "$tmp/calls"
  grep -qx "$branch activate $sha" "$tmp/calls"
done
[[ $(jq -r .commit "$tmp/opt/soramimic-video-dev/current/deploy-release.json") == "$sha" ]]
[[ $(jq -r .commit "$tmp/opt/soramimic-video-preview/current/deploy-release.json") == "$sha" ]]
[[ $(jq -r .commit "$tmp/opt/soramimic-video-public/current/deploy-release.json") == "$sha" ]]

# An unchanged branch only receives health/readiness checks; stages are not repeated.
run_controller >/dev/null
[[ $(wc -l <"$tmp/calls") -eq 9 ]]

# If a branch advances during prepare/verify, the stale SHA must not activate.
git -C "$tmp/source" switch --quiet preview
echo next >>"$tmp/source/file"
git -C "$tmp/source" add file
git -C "$tmp/source" commit --quiet -m next
git -C "$tmp/source" push --quiet "$tmp/remote.git" preview
next_sha=$(git -C "$tmp/source" rev-parse HEAD)
mark_ci_success "$next_sha"
export SORAMIMIC_AUTO_DEPLOY_TEST_ADVANCE=preview
run_controller >/dev/null
unset SORAMIMIC_AUTO_DEPLOY_TEST_ADVANCE
[[ $(wc -l <"$tmp/calls") -eq 11 ]]
[[ $(jq -r .commit "$tmp/opt/soramimic-video-preview/current/deploy-release.json") == "$sha" ]]
advanced_sha=$(git -C "$tmp/source" rev-parse HEAD)
mark_ci_success "$advanced_sha"
run_controller >/dev/null
[[ $(jq -r .commit "$tmp/opt/soramimic-video-preview/current/deploy-release.json") == "$advanced_sha" ]]
[[ $(wc -l <"$tmp/calls") -eq 14 ]]

# A branch head without a successful exact-SHA test check is not deployed or quarantined.
git -C "$tmp/source" switch --quiet dev
echo ci-pending >>"$tmp/source/file"
git -C "$tmp/source" add file
git -C "$tmp/source" commit --quiet -m ci-pending
git -C "$tmp/source" push --quiet "$tmp/remote.git" dev
dev_sha=$(git -C "$tmp/source" rev-parse HEAD)
run_controller >/dev/null 2>&1
[[ $(wc -l <"$tmp/calls") -eq 14 ]]
[[ ! -e $tmp/state/failed/dev-$dev_sha ]]
mark_ci_success "$dev_sha"
run_controller >/dev/null
[[ $(wc -l <"$tmp/calls") -eq 17 ]]
[[ $(jq -r .commit "$tmp/opt/soramimic-video-dev/current/deploy-release.json") == "$dev_sha" ]]

# A transient fetch failure neither quarantines the cached SHA nor deploys stale data.
git -C "$tmp/source" switch --quiet dev
echo transient >>"$tmp/source/file"
git -C "$tmp/source" add file
git -C "$tmp/source" commit --quiet -m transient
git -C "$tmp/source" push --quiet "$tmp/remote.git" dev
transient_sha=$(git -C "$tmp/source" rev-parse HEAD)
mark_ci_success "$transient_sha"
calls_before_fetch_failure=$(wc -l <"$tmp/calls")
export SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_FETCH_ONCE=1
run_controller >/dev/null 2>&1
unset SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_FETCH_ONCE
[[ $(wc -l <"$tmp/calls") -eq "$calls_before_fetch_failure" ]]
[[ ! -e $tmp/state/failed/dev-$transient_sha ]]
run_controller >/dev/null
[[ $(jq -r .commit "$tmp/opt/soramimic-video-dev/current/deploy-release.json") == "$transient_sha" ]]

# A failed activation is quarantined and is not retried every minute.
git -C "$tmp/source" switch --quiet main
echo failing >>"$tmp/source/file"
git -C "$tmp/source" add file
git -C "$tmp/source" commit --quiet -m failing
git -C "$tmp/source" push --quiet "$tmp/remote.git" main
failing_sha=$(git -C "$tmp/source" rev-parse HEAD)
mark_ci_success "$failing_sha"
export SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_ACTIVATE=main
if run_controller >/dev/null 2>&1; then
  echo "failed activation was reported as success" >&2
  exit 1
fi
unset SORAMIMIC_AUTO_DEPLOY_TEST_FAIL_ACTIVATE
[[ -f $tmp/state/failed/main-$failing_sha ]]
calls_after_failure=$(wc -l <"$tmp/calls")
run_controller >/dev/null 2>&1
[[ $(wc -l <"$tmp/calls") -eq "$calls_after_failure" ]]
[[ $(jq -r .commit "$tmp/opt/soramimic-video-public/current/deploy-release.json") == "$sha" ]]

# Automatic deployments never accept a branch rewind.
git -C "$tmp/source" switch --quiet --orphan unrelated
rm -f "$tmp/source/file"
echo unrelated >"$tmp/source/other"
git -C "$tmp/source" add -A
git -C "$tmp/source" commit --quiet -m unrelated
git -C "$tmp/source" push --quiet --force "$tmp/remote.git" HEAD:dev
rewind_sha=$(git -C "$tmp/source" rev-parse HEAD)
mark_ci_success "$rewind_sha"
if run_controller >"$tmp/rewind.out" 2>"$tmp/rewind.err"; then
  echo "non-fast-forward deployment was accepted" >&2
  exit 1
fi
grep -F 'refusing non-fast-forward deployment' "$tmp/rewind.err" >/dev/null
[[ $(jq -r .commit "$tmp/opt/soramimic-video-dev/current/deploy-release.json") == "$transient_sha" ]]

echo "automatic deploy tests: ok"
