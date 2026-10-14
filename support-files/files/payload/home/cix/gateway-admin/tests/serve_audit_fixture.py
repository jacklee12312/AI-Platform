"""Local-only UI acceptance fixture; never contacts the box or runs Docker.

Build frontend/dist, then run with --data-dir <temporary directory>.
The public fixture password is audit-test-password. Never deploy this script.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18780)
    parser.add_argument("--capabilities", action="store_true", help="Expose the complete local contract baseline")
    parser.add_argument("--diagnostics", action="store_true", help="Expose deterministic runtime diagnostics")
    parser.add_argument('--installation', action='store_true', help='Expose simulated model installation (no downloads/Docker)')
    parser.add_argument('--evaluations', action='store_true', help='Real evaluation tasks with fake models, no Docker')
    parser.add_argument('--experience', action='store_true', help='Local-only simulated media inference')
    args = parser.parse_args()
    from hash_password import encode
    os.environ.update(ADMIN_DATA_DIR=str(args.data_dir.resolve()), ADMIN_STATIC_DIR=str(ROOT / "frontend" / "dist"),
                      ADMIN_PASSWORD_HASH=encode("audit-test-password"), ADMIN_AGENT_TOKEN="fixture-agent",
                      GATEWAY_MANAGEMENT_TOKEN="fixture-management")
    from admin_app import main
    if args.experience:
        from experience_fixture import media, streaming
        main._experience_proxy = media
        main._stream_chat = streaming
        @main.app.get('/__fixture/experience/audio.wav')
        async def fixture_audio():
            from experience_fixture import audio_sample
            return main.Response(audio_sample(), media_type='audio/wav')
        # The production SPA catch-all is already registered; this GET fixture
        # must precede it or the test downloads HTML under a WAV filename.
        main.app.router.routes.insert(0, main.app.router.routes.pop())
    sys.path.insert(0, str(ROOT.parent / 'ai-model-gateway'))
    from app.usage import APIKeyUsageStore
    from app.access import model_pools
    import json
    registry = json.loads((ROOT.parent / 'ai-model-gateway/config/models.json').read_text(encoding='utf-8'))
    policy_models = [{'id': item['id'], 'capability': item['capability'], 'modes': item.get('modes', []),
                      'pools': model_pools(item.get('metadata', {}))} for item in registry['models']]
    ledger = APIKeyUsageStore(args.data_dir / 'fixture-usage.db')
    ledger.initialize()
    if not ledger.list_keys():
        key = ledger.create_key('LOCAL S08 示例客户（非真实 Key）')
        for index in range(105):
            ledger.record_usage(api_key_id=key['id'], request_id=f'fixture-{index}', endpoint='/v1/chat/completions', model='copaw-flash-9b',
                usage=None if index % 10 == 0 else {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 99 if index % 10 == 1 else 5},
                status='interrupted' if index % 10 == 0 else 'succeeded')
    import uvicorn
    jobs = {}
    from model_install_fixture import Fixture
    installation = Fixture(main, jobs) if args.installation else None
    from evaluation_fixture import Fixture as EvaluationFixture
    evaluation = EvaluationFixture(main, args.data_dir) if args.evaluations else None
    if installation:
        @main.app.post('/__fixture/model-install/reset')
        async def reset_installation_fixture():
            return installation.reset()
    model = {"id": "copaw-flash-9b", "object": "model", "capability": "chat", "status": "online", "enabled": True,
             "modes": ["multi-model"], "active_mode": "multi-model", "backend": "fixture"}
    host = {"system": {"cpu_cores": 12}, "containers": [{"service": "copaw", "name": "fixture-copaw", "state": "running", "status": "fixture", "image": "fixture"}],
            "modelctl": {"configured_mode": "multi-model", "detected_mode": "multi-model", "healthy": True}}

    async def agent(method, path, **kwargs):
        if evaluation and (path.startswith('/v1/evaluations') or path.startswith('/v1/jobs/')):
            return await evaluation.handle(method, path, kwargs.get('json_body') or {})
        if installation and path.startswith('/v1/model-install'):
            return await installation.handle(method, path, kwargs.get('json_body') or {})
        if method == "GET":
            if path == '/v1/diagnostics' and args.diagnostics:
                from diagnostics_fixture import fixture
                return {**fixture(args.data_dir / 'diagnostics-fixture')['collect'](), 'cached': False}
            if path == "/v1/config" and args.capabilities:
                return {"settings": {"max_body_mb": 25, "audio_max_body_mb": 100, "chat_timeout_seconds": 300,
                                     "audio_timeout_seconds": 600, "asr_max_file_mb": 100,
                                     "asr_max_audio_seconds": 3600, "asr_stream_max_seconds": 3600}}
            if path == "/v1/status":
                return host
            if path.startswith("/v1/logs?"):
                return {"lines": ["LOCAL S02 FIXTURE — no real box operations"]}
            if path.startswith("/v1/jobs/"):
                job = jobs.get(path.rsplit("/", 1)[-1])
                if job is None:
                    raise main.AgentRequestError(404, "fixture job not found")
                age = time.monotonic() - job["clock"]
                done = age >= 1
                failed = job["target"] == "gateway"
                progress = None
                if job["kind"] == "mode-switch" and not done:
                    progress = ({"phase": "loading", "stage": "正在加载 qwen3.8-27b", "current_model": "qwen3.8-27b",
                                 "elapsed_seconds": round(age, 1), "completed": 0, "total": 1} if age < .5 else
                                {"phase": "verification", "stage": "验证 qwen3.8-27b 实际推理", "current_model": "qwen3.8-27b",
                                 "elapsed_seconds": round(age, 1), "completed": 0, "total": 1})
                return {"id": job["id"], "kind": job["kind"], "target": job["target"],
                        "status": ("failed" if failed else "succeeded") if done else "running",
                        "started_at": job["created_at"], "finished_at": main._now() if done else None,
                        "progress": progress,
                        "result": {"fixture": True, "service": job["target"]} if done and not failed else None,
                        "error": "模拟健康检查失败（未执行真实重启）" if done and failed else None}
        if method == "POST" and (path.startswith("/v1/restart/") or path == "/v1/mode-switch"):
            kind = "restart" if path.startswith("/v1/restart/") else "mode-switch"
            target = path.rsplit("/", 1)[-1] if kind == "restart" else kwargs["json_body"]["mode"]
            job = {"id": uuid.uuid4().hex, "kind": kind, "target": target, "status": "queued", "created_at": main._now()}
            jobs[job["id"]] = {**job, "clock": time.monotonic()}
            return job
        raise main.HTTPException(400, "unsupported local fixture operation")

    async def gateway(method, path, **kwargs):
        if args.experience and method == 'POST' and path == '/v1/chat/completions':
            from experience_fixture import chat
            return chat(kwargs['json_body'])
        route = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(route.query).items()}
        if route.path == '/internal/usage':
            if 'api_key_id' in query: query['key_id'] = query.pop('api_key_id')
            for field in ['limit', 'offset']:
                if field in query: query[field] = int(query[field])
            return ledger.usage_report(**query)
        if route.path == '/internal/api-keys':
            if method == 'GET': return {'data': ledger.list_keys()}
            if method == 'POST': return ledger.create_key(**kwargs['json_body'])
        if route.path == '/internal/resource-policy':
            if method == 'PUT': return {'policy': ledger.set_resource_policy(kwargs['json_body'])}
            return {'policy': ledger.get_resource_policy(), 'in_flight': {'global': 0, 'key': {}, 'model': {}, 'pool': {}},
                    'models': policy_models, 'default_rpm': 120,
                    'recent_rejections': ledger.recent_rejections(), 'worker_scope': 'single-process'}
        if route.path.startswith('/internal/api-keys/'):
            key_id = route.path.rsplit('/',1)[-1]
            if method == 'PATCH': return ledger.update_key(key_id, **kwargs['json_body'])
            if method == 'DELETE': return ledger.revoke_key(key_id)
        if method == "GET" and path == "/health":
            return {"status": "ok", "service": "LOCAL S02 FIXTURE", "version": "fixture", "active_mode": "multi-model",
                    "guard_mode": "off", "online_models": 1, "models": {model["id"]: {"status": "online"}}}
        if method == "GET" and path == "/v1/models":
            if args.capabilities or args.experience:
                return {"object": "list", "data": [{key: value for key, value in {**item,
                    "status": "inactive" if item['id'] == 'qwen3.8-27b' else "online", "active_mode": "multi-model"}.items()
                    if key not in {'base_url', 'headers'}} for item in registry['models']]}
            return {"object": "list", "data": [model]}
        raise main.HTTPException(400, "inference disabled in local audit fixture")

    main._agent = agent
    main._gateway = gateway
    # Only this explicitly local fixture substitutes inference; media processing,
    # persistence, cancellation and all HTTP routes remain the real implementation.
    import asyncio
    async def batch_infer(self, item, page, **kwargs):
        await self.check_cancel(item)
        await asyncio.sleep(2)
        if item['job']['kind'] == 'ocr':
            return {'choices': [{'message': {'content': f'LOCAL BATCH OCR 第 {page} 页'}, 'finish_reason': 'stop'}]}
        return {'text': 'LOCAL BATCH ASR 转写结果', 'segments': []}
    main.BatchEngine.infer = batch_infer
    main.AUDIT_POLL_SECONDS = 0.2
    uvicorn.run(main.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    run()
