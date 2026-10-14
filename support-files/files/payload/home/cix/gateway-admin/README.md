# ModelDeck gateway administration console

ModelDeck is the local, offline administration surface for the Houmo model
gateway. It runs separately from the inference gateway on port `17780`.

The **Key 与用量** page includes per-Key permissions and quotas, shared resource controls, in-flight counts and rejection reasons. See [操作说明](../docs/manuals/ModelDeck_Key权限与资源控制.md) and [2026-09-07 implementation and verification](../docs/reports/Key权限与资源控制_2026-09-07.md).

## Surfaces

- Grouped navigation with **使用模型、接入管理、模型运维、系统交付** so
  customer integration pages are visible without mixing them with daily model
  operations. **API 参考** documents request/response fields, public errors,
  saved timeouts and SSE/WebSocket completion rules, and downloads a curated
  OpenAPI 3.1 file. See [完整 API 参考](../docs/manuals/ModelDeck_完整API参考.md).
- **运行诊断** compares actual images and model-reported slots/context against
  saved configuration and the generated version baseline, with durable deployment
  observations, fault/recovery records and JSON export. See [运行基线与诊断](../docs/manuals/ModelDeck_运行基线与诊断.md).
- Evidence-backed **能力与示例** catalog: modes, input limits, streaming contracts,
  dated verification scope, copyable Python/Bash/PowerShell requests and Markdown export.
  See [模型能力与调用示例](../docs/manuals/ModelDeck_模型能力与调用示例.md).
- Runtime dashboard with gateway, host and container health.
- Model registry and guarded-model visibility.
- Global Guard switch. It is off by default and can safely switch the gateway
  between direct chat routing and blocking input/output review.
- OpenAI-compatible streaming and non-streaming chat test console.
- Multi-Key lifecycle management with one-time plaintext display, independent
  enable/disable/revoke controls, and per-Key token accounting.
- Token usage filters and summaries by Key, model and date range, including
  `input_tokens`, `output_tokens`, `total_tokens` and recent request IDs.
- Safe switching among seven exclusive single-model LQ50 modes through the existing `modelctl` lock,
  health verification and rollback workflow.
- Allow-listed service restarts and bounded log tailing.
- Separate, confirmed device recovery with persistent evidence and fail-fast
  handling, available at the top of **模型管理**.
- Schema-driven configuration for safe gateway limits and timeouts, optional
  generation defaults, Guard modes and CPU/concurrency, ASR limits, CV default
  thresholds, and model registration/upstream settings.
- Stable, latency and concurrency presets, impact preview, affected-service
  detection, and automatic full rollback across the gateway, main model,
  Guard, and SenseVoice Compose files.
- Password login, SameSite HTTP-only sessions, CSRF protection and an SQLite
  audit trail.

The public admin process does not receive the Docker socket. It manages business
API Keys through a separate random gateway-management token; the browser never
receives this token. A separate agent
is reachable only through the internal Docker network and exposes a fixed set
of operations. It cannot execute caller-supplied shell commands.

## Device recovery / 设备故障恢复

S07/S08 operator instructions are in
[ModelDeck 操作与用量核对补充](../docs/manuals/ModelDeck_操作与用量核对补充.md).
The UI now includes Matcha/Kokoro status, bounded logs and restricted restarts;
GLM-OCR is also available in log selection. A restart job is successful only
after Docker/HTTP health verification, not merely after `docker restart` exits.
Restart, configuration, mode and recovery operations share a page-level tracker:
navigation/refresh resumes queries, ambiguous submissions stay locked until
checked, and explicit operation denials retain their HTTP status. This is not
a distributed multi-user lock; the restricted agent/device lock is authoritative.

The usage page supports Key renaming, exact model/Request ID/status filters,
100-row pagination, known-subtotal/missing/anomaly indicators and raw counter
provenance. Admin test Request IDs are forwarded to the gateway for correlation
without charging a customer Key. Existing mode/default TTS routing is unchanged.

Use **模型管理 → 设备故障恢复** when an ordinary model-container restart does
not restore inference. Read the impact notice and type `恢复设备` to submit.
This interrupts all known models sharing the Houmo NPU (including an active
exclusive model), but does not reboot the box, reset/replace the driver, rebuild
model images, change model parameters, or proactively restart CPU/CV services.

The workflow preserves all currently retained container logs, stops the same-card
models, starts only the model selected by the current mode, and verifies its
fixed functional request (including non-streaming and streaming checks for text
LLMs). No other LQ50 model is loaded alongside it.

The first fatal, load failure or incorrect probe stops further loading/inference
and stops the same-card containers. No automatic retry or rollback starts models
again after such a failure. A failed stop is explicitly reported as requiring
manual intervention. The operation shares `state/modelctl.lock` with CLI mode
switching and diagnosis; competing admin jobs are rejected, not queued.

The page displays the stage, job ID and evidence ID. Refreshing/reopening the page
resumes polling from session storage; it never resubmits recovery. Final results
are also recorded under **日志审计 → 操作审计 → 设备故障恢复**. If the agent itself
restarts, its in-memory job can be lost; follow the existing `unknown` audit
procedure instead of blindly resubmitting.

Evidence is retained on the box under
`/home/cix/houmo-docker-deployment/state/device-recovery/<evidence-id>/`, with
owner-only permissions. Raw logs can contain sensitive runtime information;
they are not served to the browser. Historical log rotations already deleted by
Docker cannot be recovered. The UI agent does not gain host kernel-log privileges.
For controlled host-side A/B tests, use the same implementation with mandatory
kernel capture (root or an equivalent authorized journal-reading account):

```text
sudo python3 /home/cix/gateway-admin/agent/device_recovery.py --plan baseline --require-kernel-logs
# Only after reviewing a successful baseline with no fatal:
sudo python3 /home/cix/gateway-admin/agent/device_recovery.py --plan parallel --require-kernel-logs
```

The historical `baseline` and `parallel` CLI plan names are accepted for script
compatibility, but both now preserve single-model semantics. They capture logs
before mutation and throughout execution and fail closed at the first fatal.

## Durable management audit (S02)

`admin_app/audit.py` commits successful SQLite transactions, rolls back failed
transactions and always closes connections. Login/logout, configuration apply,
mode switch, service restart and Key lifecycle operations are recorded. Rejected
authentication, CSRF and invalid management requests are recorded without their
request bodies. The console has one administrator identity, not per-person
accounts; unauthenticated attempts are marked `anonymous`.

Mode/config/restart submissions retain their original request ID and agent job
ID in one audit row. A server-side tracker checks pending jobs every three
seconds (at most four concurrent agent queries), even when the page is closed.
It records the final status, timestamps, result/changes and failure reason;
repeated queries cannot duplicate records or overwrite a completed result with
an older running state. Pending tracking resumes when the admin process starts.

In **日志审计 → 操作审计**, search by Request ID, job ID, target or keyword and
expand the row for details. `GET /api/audit` supports `request_id` and `job_id`
exact filters, `q` keyword search and `limit` (1–500). Completed job results can
also be returned from the audit database if the agent is unavailable.

Important boundaries:

- The agent currently keeps live jobs in memory. If it loses a pending job, the
  audit shows `unknown` (结果未知), never a fabricated success/failure; check the
  actual service before retrying. A submission transport timeout is also unknown.
- Temporary polling failures leave the job pending and are retried. The tracker
  only queries status: it never resubmits actions or runs inference.
- Passwords, session/authorization tokens and full API Keys are not intentionally
  recorded. Structured sensitive fields and known credentials are redacted;
  detail payloads are bounded valid JSON. Chat test prompts/audio/images are not
  added to the audit; the existing `api-test` entry still marks test start only.
- Startup adds `job_id` / `updated_at` and indexes to the existing database without
  deleting records. Previously uncommitted/lost records cannot be reconstructed.
  **Back up `admin-data/modeldeck.db` with SQLite's backup API before deployment.**
  Keep that pre-migration backup for rollback to older code whose positional
  eight-column inserts are incompatible with the expanded schema. Do not copy a
  live WAL database file alone. Restoring an old backup will not include new logs.

Local verification (no box access):

```text
python -m unittest discover -s gateway-admin/tests -v
python gateway-admin/tests/serve_audit_fixture.py --data-dir .codex-tmp/s02-audit-fixture --port 18780
```

The fixture binds only `127.0.0.1`, replaces both upstream clients with simulated
services, and uses the public test password `audit-test-password`. Never deploy
the fixture. After building the frontend, run `audit-local.spec.ts` with
`MODELDECK_AUDIT_FIXTURE=1` and `MODELDECK_BASE_URL=http://127.0.0.1:18780` to verify
browser interactions. The test refuses other URLs. See
[`docs/reports/S02审计修复本地验证_2026-09-04.md`](../docs/reports/S02审计修复本地验证_2026-09-04.md)
for the local verification record and deferred box acceptance.

## Build

The **批量任务** page (`/batches`) accepts up to 50 files / 250 MB per batch.
OCR supports PNG/JPEG/WebP and PDF (up to 50 pages per PDF, 200 pages per batch);
ASR accepts WAV/MP3/M4A/FLAC/OGG/AAC (up to 50 MB / 30 minutes per file), converts
them locally to 16 kHz mono PCM, then calls SenseVoice through the gateway.
See [batch API and operations](BATCH_TASKS.md) for queue semantics, limits,
backup, deployment, and tests.

Build the static frontend first:

```text
cd frontend
npm ci
npm run build
```

Before building the admin image, prepare its pinned media wheels. The appliance
uses Linux ARM64 and CPython 3.11; from `gateway-admin`, a connected build machine
can download them without installing ARM packages on Windows:

```text
python -m pip download -r requirements-batch.txt --dest wheels --only-binary=:all: --platform manylinux_2_28_aarch64 --platform manylinux_2_27_aarch64 --platform manylinux_2_17_aarch64 --python-version 311 --implementation cp --abi cp311 --abi abi3
```

Copy `wheels/` with the deployment source. Docker installs these wheels offline;
wheel binaries are ignored by Git. For another architecture/Python version,
download matching wheels instead. Local backend tests also require
`python -m pip install -r requirements-batch.txt` in their test environment.

For first-time deployment, `provision_env.py` can derive the initial
administrator password hash from the existing `HOUMO_API_KEY` without copying
the plaintext key into the admin environment. It also creates an independent
random token for the internal agent. The output file is created with mode
`0600` and is preserved on later runs unless `--force` is supplied.

After `.env` exists, build and start the two local services with Docker Compose.
The deployment is intentionally local-only and does not load fonts, scripts or
other assets from the internet.
