#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
mode="${1:-mock}"
case "$mode" in mock|real|stop) ;; *) echo 'Usage: sh deploy.sh [mock|real|stop]' >&2; exit 2 ;; esac
command -v docker >/dev/null 2>&1 || { echo 'Install Docker with the Compose plugin first.' >&2; exit 1; }
[ "$(docker info --format '{{.OSType}}')" = linux ] || { echo 'Docker must be running with Linux containers.' >&2; exit 1; }
docker compose version
if [ "$mode" = stop ] && [ ! -f .env.deploy ]; then
    echo 'Missing .env.deploy; restore the original deployment configuration first.' >&2
    exit 1
fi
if [ ! -f .env.deploy ]; then
    docker run --rm --user "$(id -u):$(id -g)" --mount "type=bind,source=$(pwd),target=/workspace" \
        --workdir /workspace python:3.12-slim python scripts/deploy/config.py
fi
set -- docker compose --env-file .env.deploy -f compose.deploy.yaml
if [ "$mode" = stop ]; then "$@" down --remove-orphans; exit; fi
if [ "$mode" = real ]; then set -- "$@" -f compose.deploy.real.yaml; fi
"$@" config --quiet
if [ "$mode" = real ]; then
    "$@" run --rm --no-deps --build dataset-init python scripts/deploy/initialize.py validate
fi
"$@" up -d --build --wait --wait-timeout 1800 --remove-orphans
"$@" ps
address=$("$@" port api 8080)
echo "FlowPay ready: http://localhost:${address##*:}"
echo 'Accounts: merchant_a, merchant_b, support_a, support_b; password: SUPPORT_DEMO_PASSWORD in .env.deploy.'
