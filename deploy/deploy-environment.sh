#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
environment=${1:-}
[[ -n $environment ]] || {
  echo "Usage: sudo deploy/deploy-environment.sh <dev|preview|main> <prepare|verify|activate> ..." >&2
  exit 2
}
shift

case $environment in
  dev)
    port=8311
    staging_port=18311
    service_user=soramimic-video-dev
    service_group=soramimic-video-dev
    ;;
  preview)
    port=8312
    staging_port=18312
    service_user=soramimic-video-preview
    service_group=soramimic-video-preview
    ;;
  main)
    port=8301
    staging_port=18301
    service_user=soramimic-video-public
    service_group=soramimic-video-public
    ;;
  *)
    echo "Unknown environment: $environment (expected dev, preview, or main)" >&2
    exit 2
    ;;
esac

export SORAMIMIC_SOURCE_REF=$environment
if [[ $environment == main ]]; then
  export SORAMIMIC_APP_ROOT=/opt/soramimic-video-public
  export SORAMIMIC_STATE_ROOT=/var/lib/soramimic-video-public/work
  export SORAMIMIC_ENV_FILE=/etc/soramimic-video/public.env
  export SORAMIMIC_SERVICE_UNIT=soramimic-video-public.service
else
  export SORAMIMIC_APP_ROOT="/opt/soramimic-video-$environment"
  export SORAMIMIC_STATE_ROOT="/var/lib/soramimic-video-$environment/work"
  export SORAMIMIC_ENV_FILE="/etc/soramimic-video/$environment.env"
  export SORAMIMIC_SERVICE_UNIT="soramimic-video-$environment.service"
fi
export SORAMIMIC_SERVICE_USER=$service_user
export SORAMIMIC_SERVICE_GROUP=$service_group
export SORAMIMIC_LISTEN_PORT=$port
export SORAMIMIC_STAGING_PORT=$staging_port
export SORAMIMIC_DEPLOY_LOCK="/run/lock/soramimic-video-$environment-deploy.lock"
export SORAMIMIC_UV_BIN=${SORAMIMIC_UV_BIN:-/usr/local/bin/uv}
export SORAMIMIC_REQUIRE_REMOTE_HEAD=1

exec "$script_dir/deploy-public.sh" "$@"
