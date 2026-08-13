#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
cp "$repo_root/deploy/deploy-environment.sh" "$tmp/deploy-environment.sh"
cat >"$tmp/deploy-public.sh" <<'EOF'
#!/usr/bin/env bash
env | sort
printf 'ARGS=%s\n' "$*"
EOF
chmod +x "$tmp/deploy-environment.sh" "$tmp/deploy-public.sh"
sha=1111111111111111111111111111111111111111

assert_mapping() {
  local branch=$1 root=$2 state=$3 env_file=$4 unit=$5 user=$6 port=$7 staging=$8 output
  output=$("$tmp/deploy-environment.sh" "$branch" prepare "$sha")
  grep -qx "SORAMIMIC_SOURCE_REF=$branch" <<<"$output"
  grep -qx "SORAMIMIC_APP_ROOT=$root" <<<"$output"
  grep -qx "SORAMIMIC_STATE_ROOT=$state" <<<"$output"
  grep -qx "SORAMIMIC_ENV_FILE=$env_file" <<<"$output"
  grep -qx "SORAMIMIC_SERVICE_UNIT=$unit" <<<"$output"
  grep -qx "SORAMIMIC_SERVICE_USER=$user" <<<"$output"
  grep -qx "SORAMIMIC_SERVICE_GROUP=$user" <<<"$output"
  grep -qx "SORAMIMIC_LISTEN_PORT=$port" <<<"$output"
  grep -qx "SORAMIMIC_STAGING_PORT=$staging" <<<"$output"
  grep -qx 'SORAMIMIC_REQUIRE_REMOTE_HEAD=1' <<<"$output"
  grep -qx "ARGS=prepare $sha" <<<"$output"
}

assert_mapping dev /opt/soramimic-video-dev /var/lib/soramimic-video-dev/work \
  /etc/soramimic-video/dev.env soramimic-video-dev.service soramimic-video-dev 8311 18311
assert_mapping preview /opt/soramimic-video-preview /var/lib/soramimic-video-preview/work \
  /etc/soramimic-video/preview.env soramimic-video-preview.service \
  soramimic-video-preview 8312 18312
assert_mapping main /opt/soramimic-video-public /var/lib/soramimic-video-public/work \
  /etc/soramimic-video/public.env soramimic-video-public.service \
  soramimic-video-public 8301 18301

if "$tmp/deploy-environment.sh" production prepare "$sha" >/dev/null 2>&1; then
  echo "unknown environment was accepted" >&2
  exit 1
fi

for environment in dev preview public; do
  unit="$repo_root/deploy/systemd/soramimic-video-$environment.service"
  grep -qx "User=soramimic-video-$environment" "$unit"
  grep -qx "Group=soramimic-video-$environment" "$unit"
  grep -qx 'SupplementaryGroups=soramimic-video' "$unit"
done
grep -qx 'ExecStart=/usr/local/sbin/soramimic-video-auto-deploy' \
  "$repo_root/deploy/systemd/soramimic-video-auto-deploy.service"
grep -qx 'OnUnitInactiveSec=1min' \
  "$repo_root/deploy/systemd/soramimic-video-auto-deploy.timer"

echo "environment deploy mapping tests: ok"
