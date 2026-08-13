#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
installer="$repo_root/deploy/install-auto-deploy.sh"
controller_unit="$repo_root/deploy/systemd/soramimic-video-auto-deploy.service"

[[ $EUID -ne 0 ]] && {
  if "$installer" --confirm 1111111111111111111111111111111111111111 >/dev/null 2>&1; then
    echo "installer accepted a non-root invocation" >&2
    exit 1
  fi
}

grep -Fq 'checkout HEAD does not match --confirm' "$installer"
grep -Fq 'confirmed SHA is not in origin/dev' "$installer"
grep -Fq 'rollback_migration' "$installer"
grep -Fq 'find "$state" -xdev -user "soramimic-video-$environment"' "$installer"
grep -Fq 'chown "${old_owner[$environment]}"' "$installer"
grep -Fq 'service on port $port did not become healthy' "$installer"
grep -qx 'ProtectSystem=strict' "$controller_unit"
grep -qx 'TimeoutStartSec=infinity' "$controller_unit"
grep -qx 'TimeoutStopSec=infinity' "$controller_unit"

echo "automatic deploy installer tests: ok"
