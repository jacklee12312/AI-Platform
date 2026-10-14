#!/usr/bin/env bash
set -u

DURATION_SECONDS="${1:-28800}"
INTERVAL_SECONDS="${2:-60}"
DEPLOY_DIR="${HOUMO_DEPLOY_DIR:-/home/cix/houmo-docker-deployment}"
STATE_DIR="${HOUMO_STATE_DIR:-${DEPLOY_DIR}/state}"
MODE_FILE="${STATE_DIR}/active-mode"
ENV_FILE="${DEPLOY_DIR}/.env"
GATEWAY_URL="${GATEWAY_URL:-http://192.168.133.1:17700}"

if ! [[ "${DURATION_SECONDS}" =~ ^[0-9]+$ && "${INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    printf 'duration and interval must be integer seconds\n' >&2
    exit 2
fi

mkdir -p "${STATE_DIR}"
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

RUN_ID="$(date +%Y%m%dT%H%M%S)"
LOG_FILE="${STATE_DIR}/soak-${RUN_ID}.jsonl"
SUMMARY_FILE="${STATE_DIR}/soak-${RUN_ID}.summary"
BODY_FILE="$(mktemp "${STATE_DIR}/soak-body.XXXXXX")"
trap 'rm -f "${BODY_FILE}"' EXIT

deadline=$(( $(date +%s) + DURATION_SECONDS ))
checks=0
failures=0

while (( $(date +%s) < deadline )); do
    mode="$(tr -d '[:space:]' < "${MODE_FILE}" 2>/dev/null || printf unknown)"
    case "${mode}" in
        copaw) model="copaw-flash-9b" ;;
        spark-x1) model="spark-x1-4b" ;;
        qwen38) model="qwen3.8-27b" ;;
        qwen-vl) model="qwen3-vl-8b" ;;
        *) printf 'soak-test requires a chat-capable mode; current=%s\n' "${mode}" >&2; exit 2 ;;
    esac

    health_result="$(curl --silent --show-error --max-time 15 --output "${BODY_FILE}" \
        --write-out '%{http_code} %{time_total}' "${GATEWAY_URL}/health" 2>/dev/null || printf '000 15')"
    health_code="${health_result%% *}"
    health_time="${health_result#* }"

    chat_result="$(curl --silent --show-error --max-time 300 --output "${BODY_FILE}" \
        --write-out '%{http_code} %{time_total}' \
        -H "Authorization: Bearer ${HOUMO_API_KEY}" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK only.\"}],\"temperature\":0,\"max_tokens\":16}" \
        "${GATEWAY_URL}/v1/chat/completions" 2>/dev/null || printf '000 300')"
    chat_code="${chat_result%% *}"
    chat_time="${chat_result#* }"

    result="pass"
    if [[ "${health_code}" != "200" || "${chat_code}" != "200" ]]; then
        result="fail"
        failures=$((failures + 1))
    fi
    checks=$((checks + 1))
    printf '{"ts":"%s","mode":"%s","model":"%s","health_code":"%s","health_seconds":%s,"chat_code":"%s","chat_seconds":%s,"result":"%s"}\n' \
        "$(date --iso-8601=seconds)" "${mode}" "${model}" "${health_code}" "${health_time}" \
        "${chat_code}" "${chat_time}" "${result}" >> "${LOG_FILE}"

    sleep "${INTERVAL_SECONDS}"
done

printf 'run_id=%s\nchecks=%s\nfailures=%s\nresult=%s\nlog=%s\n' \
    "${RUN_ID}" "${checks}" "${failures}" "$([[ ${failures} -eq 0 ]] && printf PASS || printf FAIL)" "${LOG_FILE}" \
    | tee "${SUMMARY_FILE}"

[[ ${failures} -eq 0 ]]
