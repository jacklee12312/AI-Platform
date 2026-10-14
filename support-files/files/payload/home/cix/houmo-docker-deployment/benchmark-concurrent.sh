#!/bin/sh
set -eu

cd "$(dirname "$0")"
set -a
. ./.env
set +a

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
api_host=${HOUMO_API_HOST:-192.168.133.1}
gateway_url="http://${api_host}:17700"
auth_header="Authorization: Bearer ${HOUMO_API_KEY}"
active_mode=$(tr -d '[:space:]' < ./state/active-mode)
if [ "$active_mode" != copaw ]; then
  printf 'benchmark requires copaw mode; current=%s\n' "$active_mode" >&2
  exit 2
fi
chat_body='{"model":"copaw-flash-9b","messages":[{"role":"user","content":"请写一段约五百字的中文说明，主题是边缘AI部署，保持连贯，不要提前结束。"}],"temperature":0,"max_tokens":96}'

chat() {
  label="$1"
  curl --fail --silent --show-error --max-time 300 \
    --output "$work_dir/$label.json" --write-out '%{time_total}' \
    --header "$auth_header" --header 'Content-Type: application/json' \
    --data "$chat_body" "${gateway_url}/v1/chat/completions" \
    > "$work_dir/$label.time"
}

chat idle

chat concurrent_1 &
chat_pid_1=$!
chat concurrent_2 &
chat_pid_2=$!
wait "$chat_pid_1"
wait "$chat_pid_2"

python3 - "$work_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for label in ("idle", "concurrent_1", "concurrent_2"):
    data = json.loads((root / f"{label}.json").read_text())
    wall = float((root / f"{label}.time").read_text())
    usage = data.get("usage", {})
    tokens = int(usage.get("completion_tokens") or 0)
    timings = data.get("timings", {})
    server_rate = timings.get("predicted_per_second")
    print(f"{label}_http_wall_seconds={wall:.3f}")
    print(f"{label}_completion_tokens={tokens}")
    print(f"{label}_wall_completion_tokens_per_second={(tokens / wall if wall else 0):.2f}")
    if server_rate is not None:
        print(f"{label}_server_decode_tokens_per_second={float(server_rate):.2f}")
print("concurrent_chat_requests=2")
print("concurrent_failures=0")
PY
