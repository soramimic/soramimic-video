#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 2; }
[[ ${1:-} == --confirm && ${2:-} =~ ^[0-9a-f]{40}$ && $# -eq 2 ]] || {
  echo "Usage: sudo deploy/install-auto-deploy.sh --confirm <merged-dev-commit-SHA>" >&2
  exit 2
}
confirmed=$2
source_dir=$(cd "$(dirname "$0")/.." && pwd)
shared_group=soramimic-video
repo_url=https://github.com/soramimic/soramimic-video.git
environments=(dev preview public)
backup_dir=''
migration_started=0
migration_complete=0
timer_was_enabled=0

declare -A old_owner

rollback_migration() {
  local environment unit state failed=0
  if (( migration_started && ! migration_complete )); then
    echo "installation failed; restoring service units and state ownership" >&2
    for environment in "${environments[@]}"; do
      systemctl stop "soramimic-video-$environment.service" >/dev/null 2>&1 || true
    done
    for environment in "${environments[@]}"; do
      unit="soramimic-video-$environment.service"
      state="/var/lib/soramimic-video-$environment/work"
      install -o root -g root -m 0644 "$backup_dir/$unit" "/etc/systemd/system/$unit" || failed=1
      find "$state" -xdev -user "soramimic-video-$environment" -exec \
        chown "${old_owner[$environment]}" {} + || failed=1
    done
    systemctl daemon-reload || failed=1
    for environment in "${environments[@]}"; do
      systemctl start "soramimic-video-$environment.service" || failed=1
    done
  fi
  (( timer_was_enabled == 0 )) || systemctl enable --now \
    soramimic-video-auto-deploy.timer >/dev/null 2>&1 || failed=1
  (( failed == 0 )) || echo "automatic rollback was incomplete; inspect all three services" >&2
}
trap rollback_migration EXIT

for command in git install systemctl curl jq chown useradd usermod groupadd flock; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done
[[ -x /usr/local/bin/uv ]] || { echo "missing /usr/local/bin/uv" >&2; exit 1; }
[[ $(git -c safe.directory="$source_dir" -C "$source_dir" rev-parse HEAD) == "$confirmed" ]] || {
  echo "checkout HEAD does not match --confirm" >&2; exit 1;
}
[[ -z $(git -c safe.directory="$source_dir" -C "$source_dir" status \
  --porcelain --untracked-files=no) ]] || { echo "checkout has tracked changes" >&2; exit 1; }
[[ $(git -c safe.directory="$source_dir" -C "$source_dir" remote get-url origin) == "$repo_url" ]] || {
  echo "unexpected checkout origin" >&2; exit 1;
}
git -c safe.directory="$source_dir" -C "$source_dir" fetch --quiet origin dev
git -c safe.directory="$source_dir" -C "$source_dir" merge-base --is-ancestor \
  "$confirmed" refs/remotes/origin/dev || { echo "confirmed SHA is not in origin/dev" >&2; exit 1; }

for environment in "${environments[@]}"; do
  [[ -d /var/lib/soramimic-video-$environment/work ]] || {
    echo "missing state directory for $environment" >&2; exit 1;
  }
  [[ -f /etc/systemd/system/soramimic-video-$environment.service ]] || {
    echo "missing installed service unit for $environment" >&2; exit 1;
  }
done

if systemctl is-enabled soramimic-video-auto-deploy.timer >/dev/null 2>&1; then
  timer_was_enabled=1
fi
systemctl disable --now soramimic-video-auto-deploy.timer >/dev/null 2>&1 || true
systemctl stop soramimic-video-auto-deploy.service >/dev/null 2>&1 || true
exec 9>/run/lock/soramimic-video-auto-deploy.lock
flock 9

create_service_user() {
  local name=$1
  getent group "$name" >/dev/null || groupadd --system "$name"
  id "$name" >/dev/null 2>&1 || useradd --system --gid "$name" \
    --groups "$shared_group" --home-dir /nonexistent --shell /usr/sbin/nologin "$name"
  usermod -a -G "$shared_group" "$name"
}
for name in soramimic-video-dev soramimic-video-preview soramimic-video-public; do
  create_service_user "$name"
done
getent group soramimic-video-deployer >/dev/null || groupadd --system soramimic-video-deployer
id soramimic-video-deployer >/dev/null 2>&1 || useradd --system \
  --gid soramimic-video-deployer --home-dir /nonexistent --shell /usr/sbin/nologin \
  soramimic-video-deployer

backup_dir=$(mktemp -d /var/tmp/soramimic-video-units.XXXXXX)
chmod 0700 "$backup_dir"
for environment in "${environments[@]}"; do
  unit="soramimic-video-$environment.service"
  state="/var/lib/soramimic-video-$environment/work"
  cp -a "/etc/systemd/system/$unit" "$backup_dir/$unit"
  old_owner[$environment]=$(stat -c %U "$state")
done

install -d -m 0755 /usr/local/libexec/soramimic-video-deploy
for file in deploy-public.sh deploy-environment.sh public-deploy-lib.sh; do
  install -o root -g root -m 0755 "$source_dir/deploy/$file" \
    "/usr/local/libexec/soramimic-video-deploy/$file"
done
install -o root -g root -m 0755 "$source_dir/deploy/auto-deploy-branches.sh" \
  /usr/local/sbin/soramimic-video-auto-deploy
install -o root -g root -m 0644 \
  "$source_dir/deploy/systemd/soramimic-video-auto-deploy.service" \
  /etc/systemd/system/soramimic-video-auto-deploy.service
install -o root -g root -m 0644 \
  "$source_dir/deploy/systemd/soramimic-video-auto-deploy.timer" \
  /etc/systemd/system/soramimic-video-auto-deploy.timer
install -o root -g root -m 0644 \
  "$source_dir/deploy/systemd/soramimic-video-assets-sync.service" \
  /etc/systemd/system/soramimic-video-assets-sync.service
for unit in soramimic-video-assets-sync.timer \
  soramimic-video-assets-manifest-sync.service \
  soramimic-video-assets-manifest-sync.timer; do
  install -o root -g root -m 0644 "$source_dir/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done

migration_started=1
for environment in "${environments[@]}"; do
  systemctl stop "soramimic-video-$environment.service"
done
for environment in "${environments[@]}"; do
  unit="soramimic-video-$environment.service"
  state="/var/lib/soramimic-video-$environment/work"
  install -o root -g root -m 0644 "$source_dir/deploy/systemd/$unit" "/etc/systemd/system/$unit"
  find "$state" -xdev -user "${old_owner[$environment]}" -exec \
    chown "soramimic-video-$environment" {} +
done
systemctl daemon-reload
for environment in "${environments[@]}"; do
  systemctl start "soramimic-video-$environment.service"
done

for port in 8311 8312 8301; do
  healthy=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$port/healthz" |
      jq -e '.status == "ok"' >/dev/null; then
      healthy=1
      break
    fi
    sleep 1
  done
  (( healthy )) || { echo "service on port $port did not become healthy" >&2; exit 1; }
done
systemd-run --quiet --wait --pipe --collect --property=Type=oneshot \
  --property=User=soramimic-video --property=Group=soramimic-video \
  --property=SupplementaryGroups=soramimic-video-dev \
  /usr/bin/test -x /opt/soramimic-video-dev/current/.venv/bin/soramimic-video
runuser -u soramimic-video-dev -- test -r /var/lib/soramimic-video-assets/manifest.json
runuser -u soramimic-video-preview -- test -r /var/lib/soramimic-video-assets/manifest.json
runuser -u soramimic-video-public -- test -r /var/lib/soramimic-video-assets/manifest.json

migration_complete=1
rm -rf -- "$backup_dir"
trap - EXIT
install -d -m 0750 -o soramimic-video-deployer -g soramimic-video-deployer \
  /var/lib/soramimic-video-deployer
printf '%s  %s\n' "$confirmed" "$(sha256sum /usr/local/sbin/soramimic-video-auto-deploy | cut -d' ' -f1)" \
  >/var/lib/soramimic-video-deployer/installed-version
chmod 0644 /var/lib/soramimic-video-deployer/installed-version
systemctl enable --now soramimic-video-auto-deploy.timer
systemctl enable --now soramimic-video-assets-sync.timer \
  soramimic-video-assets-manifest-sync.timer
flock -u 9
systemctl start soramimic-video-auto-deploy.service
echo "installed automatic deployment controller and isolated service users at $confirmed"
