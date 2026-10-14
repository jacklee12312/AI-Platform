#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="${HOUMO_DEPLOY_DIR:-/home/cix/houmo-docker-deployment}"
GATEWAY_DIR="${AI_GATEWAY_DIR:-/home/cix/ai-model-gateway}"

set -a
# shellcheck disable=SC1090
. "${DEPLOY_DIR}/.env"
set +a

exec python3 "${GATEWAY_DIR}/tests/integration_acceptance.py" "$@"
