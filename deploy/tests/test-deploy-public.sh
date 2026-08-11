#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
deploy="$repo_root/deploy/deploy-public.sh"
rollback="$repo_root/deploy/rollback-public.sh"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/app/releases" "$tmp/app/deployments"

cat >"$tmp/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
if [[ $1 == show ]]; then
  case $* in
    *--property=Environment*)
      [[ ${MISSING_LOCAL_OPS:-0} == 1 ]] || printf 'SORAMIMIC_ALLOW_LOCAL_OPS=1\n'
      ;;
    *--property=ActiveState*) printf 'active\n' ;;
    *--property=MainPID*) printf '4242\n' ;;
  esac
fi
exit 0
EOF
cat >"$tmp/bin/systemd-run" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$tmp/bin/ss" <<'EOF'
#!/usr/bin/env bash
if [[ ${BUSY_PORT:-0} == 1 ]]; then
  printf 'LISTEN 0 128 127.0.0.1:18301\n'
elif [[ " $* " == *" -ltnp "* ]]; then
  printf 'LISTEN 0 128 127.0.0.1:18301 users:(("python",pid=4242,fd=3))\n'
fi
EOF
cat >"$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=${*: -1}
if [[ ${FAIL_BAD_RELEASE:-0} == 1 && -L ${CURRENT_LINK:?} && \
      $(readlink -f "$CURRENT_LINK") == *bad* && $url == */healthz ]]; then
  exit 22
fi
case $url in
  */healthz) printf '{"status":"ok"}\n' ;;
  */readyz) printf '{"status":"ready","checks":{"jobs_dir_writable":true}}\n' ;;
  */api/config)
    if [[ ${SIMPLE_UI:-0} == 1 ]]; then
      if [[ ${BAD_SIMPLE_CONFIG:-0} == 1 ]]; then
        printf '{"simple_ui":true,"editor":true}\n'
      else
        printf '{"simple_ui":true,"editor":false}\n'
      fi
    else
      printf '{"editor":true}\n'
    fi
    ;;
  */editor/wordlists/scientist.csv)
    if [[ ${FAIL_ODA:-0} == 1 && $(readlink -f "$CURRENT_LINK") == *bad* ]]; then
      printf '335,小田稔,おだ,おだ,family,物理\n'
    else
      printf '335,小田稔,小田,おだ,family,物理\n'
    fi
    ;;
  */logo-soramimic-v1.png)
    if [[ ${FAIL_PNG:-0} == 1 && $(readlink -f "$CURRENT_LINK") == *bad* ]]; then
      printf 'not-png'
    else
      printf '\211PNG\r\n\032\n'
    fi
    ;;
  */editor/editor.html)
    if [[ ${SIMPLE_UI:-0} == 1 ]]; then
      if [[ $* == *'%{http_code}'* ]]; then
        [[ ${LEAK_EDITOR:-0} == 1 ]] && printf '200' || printf '404'
      else
        exit 22
      fi
    else
      [[ $* == *'%{http_code}'* ]] && printf '200' || printf '<html></html>\n'
    fi
    ;;
  */)
    if [[ ${FAIL_INDEX:-0} == 1 && $(readlink -f "$CURRENT_LINK") == *bad* ]]; then
      printf '<html>missing brand</html>\n'
    else
      printf '<img src="/logo-soramimic-v1.png">\n'
    fi
    ;;
  *) exit 22 ;;
esac
EOF
chmod +x "$tmp/bin/systemctl" "$tmp/bin/systemd-run" "$tmp/bin/ss" "$tmp/bin/curl"

sha1=1111111111111111111111111111111111111111
sha2=2222222222222222222222222222222222222222
make_release() {
  local sha=$1 name=$2 dir
  dir="$tmp/app/releases/$name"
  mkdir -p "$dir/.venv/bin" "$dir/external/soramimic/frontend/dist"
  : >"$dir/.venv/bin/soramimic-video"
  : >"$dir/external/soramimic/frontend/dist/editor.html"
  chmod +x "$dir/.venv/bin/soramimic-video"
  jq -n --arg commit "$sha" --arg release_dir "$dir" \
    '{commit:$commit,release_dir:$release_dir}' >"$dir/deploy-release.json"
  local manifest="$tmp/app/deployments/manifest-$sha.sha256"
  (cd "$dir" && find . -type f -print0 | sort -z | xargs -0 sha256sum) >"$manifest"
  jq -n --arg commit "$sha" --arg release_dir "$dir" --arg manifest "$manifest" \
    --arg manifest_sha256 "$(sha256sum "$manifest" | cut -d' ' -f1)" \
    --arg symlink_sha256 "$(cd "$dir" && find . -type l -printf '%p\0%l\0' | sort -z | sha256sum | cut -d' ' -f1)" \
    '{commit:$commit,release_dir:$release_dir,manifest:$manifest,
      manifest_sha256:$manifest_sha256,symlink_sha256:$symlink_sha256}' \
    >"$tmp/app/deployments/prepared-$sha.json"
  jq -n --arg commit "$sha" --arg release_dir "$dir" \
    --arg manifest_sha256 "$(sha256sum "$manifest" | cut -d' ' -f1)" \
    '{commit:$commit,release_dir:$release_dir,manifest_sha256:$manifest_sha256}' \
    >"$tmp/app/deployments/verified-$sha.json"
}
make_release "$sha1" "legacy-${sha1:0:12}"
make_release "$sha2" bad
mkdir -p "$tmp/proc/4242"
ln -s "$tmp/app/releases/bad" "$tmp/proc/4242/cwd"
ln -s "$tmp/app/releases/legacy-${sha1:0:12}" "$tmp/app/current"

common_env=(
  PATH="$tmp/bin:$PATH"
  SORAMIMIC_ALLOW_NON_ROOT_TEST=1
  SORAMIMIC_APP_ROOT="$tmp/app"
  SORAMIMIC_CURRENT_LINK="$tmp/app/current"
  SORAMIMIC_HEALTH_ATTEMPTS=1
  CURRENT_LINK="$tmp/app/current"
  SORAMIMIC_DEPLOY_LOCK="$tmp/deploy.lock"
  SORAMIMIC_SERVICE_USER="$(id -un)"
  SORAMIMIC_SERVICE_GROUP="$(id -gn)"
  SORAMIMIC_STATE_ROOT="$tmp/state"
  SORAMIMIC_ENV_FILE="$tmp/public.env"
  SORAMIMIC_PROC_ROOT="$tmp/proc"
)
: >"$tmp/public.env"

# Deploy and rollback share one non-blocking host lock.
exec 8>"$tmp/deploy.lock"
flock -n 8
if env "${common_env[@]}" "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null 2>&1; then
  echo "deploy unexpectedly ignored the shared lock" >&2
  exit 1
fi
flock -u 8
exec 8>&-

if env "${common_env[@]}" "$deploy" activate "$sha2" >/dev/null 2>&1; then
  echo "activate without confirmation unexpectedly succeeded" >&2
  exit 1
fi

env "${common_env[@]}" "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/bad" ]]

# Simple UI must attest that editor is disabled in config and returns an actual 404.
ln -sfn "$tmp/app/releases/legacy-${sha1:0:12}" "$tmp/app/current"
env "${common_env[@]}" SIMPLE_UI=1 "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/bad" ]]
ln -sfn "$tmp/app/releases/legacy-${sha1:0:12}" "$tmp/app/current"
for simple_failure in BAD_SIMPLE_CONFIG LEAK_EDITOR; do
  if env "${common_env[@]}" SIMPLE_UI=1 "$simple_failure=1" \
    "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null 2>&1; then
    echo "$simple_failure unexpectedly passed simple UI smoke" >&2
    exit 1
  fi
  [[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]
done

# A failed health check restores the exact previous symlink target.
ln -sfn "$tmp/app/releases/legacy-${sha1:0:12}" "$tmp/app/current"
if env "${common_env[@]}" FAIL_BAD_RELEASE=1 "$deploy" activate "$sha2" --confirm "$sha2" \
  >/dev/null 2>&1; then
  echo "unhealthy activation unexpectedly succeeded" >&2
  exit 1
fi
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]

# Installed unit readiness access is a hard preflight before current changes.
if env "${common_env[@]}" MISSING_LOCAL_OPS=1 \
  "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null 2>&1; then
  echo "activation without local readiness unexpectedly succeeded" >&2
  exit 1
fi
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]

# A stale listener prevents verify before any transient unit can be trusted.
rm -f "$tmp/app/deployments/verified-$sha2.json"
if env "${common_env[@]}" BUSY_PORT=1 "$deploy" verify "$sha2" >/dev/null 2>&1; then
  echo "verify with occupied staging port unexpectedly succeeded" >&2
  exit 1
fi
[[ ! -e $tmp/app/deployments/verified-$sha2.json ]]
# The expected transient MainPID/cwd/listener identity can produce the attestation.
env "${common_env[@]}" "$deploy" verify "$sha2" >/dev/null
[[ -e $tmp/app/deployments/verified-$sha2.json ]]

# Every surface assertion must enter activate's rollback path, not exit from inside smoke.
for failure in FAIL_INDEX FAIL_ODA FAIL_PNG; do
  ln -sfn "$tmp/app/releases/legacy-${sha1:0:12}" "$tmp/app/current"
  if env "${common_env[@]}" "$failure=1" \
    "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null 2>&1; then
    echo "$failure activation unexpectedly succeeded" >&2
    exit 1
  fi
  [[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]
done

# Explicit release rollback also requires exact confirmation.
ln -sfn "$tmp/app/releases/bad" "$tmp/app/current"
env "${common_env[@]}" "$rollback" release --to "$sha1" --confirm "$sha1" >/dev/null
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]

# First migration can register a metadata-less legacy release as previous.
rm "$tmp/app/releases/legacy-${sha1:0:12}/deploy-release.json"
rm "$tmp/app/deployments/prepared-$sha1.json"
printf '%s\n' "$sha1" >"$tmp/app/releases/legacy-${sha1:0:12}/REVISION"
env "${common_env[@]}" "$deploy" activate "$sha2" --confirm "$sha2" \
  --previous-sha "$sha1" >/dev/null
env "${common_env[@]}" "$rollback" release --confirm "$sha1" >/dev/null
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]

grep -q 'Environment=SORAMIMIC_ALLOW_LOCAL_OPS=1' \
  "$repo_root/deploy/systemd/soramimic-video-public.service"

# uv override must be an explicit executable absolute path, never a PATH fragment.
if env "${common_env[@]}" SORAMIMIC_UV_BIN=uv \
  "$deploy" --dry-run prepare "$sha1" >/dev/null 2>"$tmp/uv-error"; then
  echo "relative uv path unexpectedly succeeded" >&2
  exit 1
fi
grep -q 'uvの絶対path' "$tmp/uv-error"

# uv console scripts are only valid when their absolute shebang uses the final path.
shebang_release="$tmp/shebang-release"
mkdir -p "$shebang_release/.venv/bin"
printf '#!%s/.venv/bin/python\n' "$shebang_release" \
  >"$shebang_release/.venv/bin/soramimic-video"
(
  # shellcheck source=deploy/public-deploy-lib.sh
  source "$repo_root/deploy/public-deploy-lib.sh"
  assert_final_venv_shebang "$shebang_release"
)
printf '#!/tmp/.incoming-release/.venv/bin/python\n' \
  >"$shebang_release/.venv/bin/soramimic-video"
if (
  # shellcheck source=deploy/public-deploy-lib.sh
  source "$repo_root/deploy/public-deploy-lib.sh"
  assert_final_venv_shebang "$shebang_release"
) >/dev/null 2>&1; then
  echo "relocated venv shebang unexpectedly passed" >&2
  exit 1
fi

# Prepared content is immutable by policy and hash-checked again at activate time.
chmod u+w "$tmp/app/releases/bad/deploy-release.json"
printf '\n' >>"$tmp/app/releases/bad/deploy-release.json"
if env "${common_env[@]}" "$deploy" activate "$sha2" --confirm "$sha2" >/dev/null 2>&1; then
  echo "tampered release unexpectedly activated" >&2
  exit 1
fi
[[ $(readlink -f "$tmp/app/current") == "$tmp/app/releases/legacy-${sha1:0:12}" ]]

if command -v shellcheck >/dev/null; then
  shellcheck "$repo_root/deploy/"*.sh
fi
echo "deploy shell tests: ok"
