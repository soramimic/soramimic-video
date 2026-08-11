#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
environment=${1:-}
[[ -n $environment ]] || {
  echo "Usage: sudo deploy/deploy-environment.sh <dev|preview> <prepare|verify|activate> ..." >&2
  exit 2
}
shift

case $environment in
  dev)
    port=8311
    staging_port=18311
    ;;
  preview)
    port=8312
    staging_port=18312
    ;;
  *)
    echo "Unknown environment: $environment (expected dev or preview)" >&2
    exit 2
    ;;
esac

export SORAMIMIC_SOURCE_REF=$environment
export SORAMIMIC_APP_ROOT="/opt/soramimic-video-$environment"
export SORAMIMIC_STATE_ROOT="/var/lib/soramimic-video-$environment/work"
export SORAMIMIC_ENV_FILE="/etc/soramimic-video/$environment.env"
export SORAMIMIC_SERVICE_UNIT="soramimic-video-$environment.service"
export SORAMIMIC_LISTEN_PORT=$port
export SORAMIMIC_STAGING_PORT=$staging_port
export SORAMIMIC_DEPLOY_LOCK="/run/lock/soramimic-video-$environment-deploy.lock"
export SORAMIMIC_UV_BIN=${SORAMIMIC_UV_BIN:-/usr/local/bin/uv}

exec "$script_dir/deploy-public.sh" "$@"
