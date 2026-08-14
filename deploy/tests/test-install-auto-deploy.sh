#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
installer="$repo_root/deploy/install-auto-deploy.sh"
controller_unit="$repo_root/deploy/systemd/soramimic-video-auto-deploy.service"
manifest_sync_unit="$repo_root/deploy/systemd/soramimic-video-assets-manifest-sync.service"
manifest_sync_timer="$repo_root/deploy/systemd/soramimic-video-assets-manifest-sync.timer"
full_sync_unit="$repo_root/deploy/systemd/soramimic-video-assets-sync.service"
full_sync_timer="$repo_root/deploy/systemd/soramimic-video-assets-sync.timer"

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
grep -Fq 'soramimic-video-assets-manifest-sync.timer' "$installer"
grep -Fq 'soramimic-video-assets-sync.timer' "$installer"
grep -Fq 'sync-assets --mode manifest' "$manifest_sync_unit"
grep -Fq 'OnCalendar=daily' "$manifest_sync_timer"
grep -Fq 'sync-assets --mode full' "$full_sync_unit"
grep -Fq 'OnCalendar=monthly' "$full_sync_timer"

echo "automatic deploy installer tests: ok"
