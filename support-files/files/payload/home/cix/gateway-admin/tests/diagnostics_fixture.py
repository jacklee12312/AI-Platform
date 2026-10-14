"""Deterministic diagnostic inputs; no Docker, network or model operations."""
import hashlib
import json
import subprocess
from pathlib import Path
from agent.configuration import ALL_BINDINGS, RuntimeSettings
from agent.diagnostics import BASELINE, collect
from agent.main import CONTAINERS, HEALTH_PORTS

SECRET = 'fixture-credential-must-never-be-exported'


def fixture(root: Path):
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    components = baseline['components']; specs = baseline['models']
    names = {**CONTAINERS, 'admin': 'gateway-admin', 'agent': 'gateway-admin-agent'}
    from agent.diagnostics import MODEL_COMPONENTS
    defaults = RuntimeSettings().model_dump(); saved = {'gateway': {'services': {}}, 'houmo': {'services': {}}, 'sensevoice': {'services': {}}}
    raw = []; images = []
    for service, name in names.items():
        spec = next((v for v in specs.values() if v.get('compose_service') == service and v.get('gateway_enabled')), {})
        component = MODEL_COMPONENTS.get(service) or spec.get('service_component')
        image = components[component]['image']; identity = 'sha256:' + hashlib.sha256(service.encode()).hexdigest()
        env = {b.env_key: b.encode(defaults[b.setting]) for b in ALL_BINDINGS if ('qwen3guard' if b.service == 'guard' else b.service) == service}
        env.update(HOUMO_API_KEY=SECRET, UNKNOWN_SECRET=SECRET)
        cmd = ['--api-key', SECRET]
        if spec.get('concurrency'): cmd += ['-np', str(1 if service == 'copaw' else spec['concurrency'])]
        if spec.get('compose_context'): cmd += ['-c', str(spec['compose_context'])]
        running = service != 'qwen38'
        item = {'Name': '/' + name, 'Id': hashlib.sha256(name.encode()).hexdigest(), 'Image': identity, 'Created': '2026-09-07T00:00:00Z',
                'Config': {'Image': image, 'Cmd': cmd, 'Env': [k+'='+str(v) for k,v in env.items()]}, 'RestartCount': 0,
                'State': {'Status': 'running' if running else 'exited', 'Running': running, 'StartedAt': '2026-09-07T00:00:01Z', 'ExitCode': 0, 'OOMKilled': False,
                          'Health': {'Status': 'healthy' if running else 'unhealthy'}},
                'HostConfig': {'Memory': 268435456, 'NanoCpus': 500000000, 'ReadonlyRootfs': True},
                'NetworkSettings': {'Networks': {'local': {'IPAddress': '172.18.0.2'}}}}
        raw.append(item); images.append({'Id': identity, 'RepoDigests': []})
        if service not in {'admin', 'agent'}:
            group = service if service in {'gateway', 'sensevoice'} else 'houmo'
            entry = {'image': image, 'command': list(cmd), 'environment': dict(env)}
            if service == 'copaw': entry['command'] = ['-np', '2']
            if service == 'gateway': entry['environment']['MAX_BODY_BYTES'] = str(20 * 1024 * 1024)
            saved[group]['services'][service] = entry
    paths = {}
    for group in saved:
        folder = root / group; folder.mkdir(parents=True, exist_ok=True); paths[group] = folder / 'compose.yaml'
        paths[group].write_text('services: {}\n', encoding='utf-8')
    (root/'houmo/state').mkdir(exist_ok=True)
    (root/'houmo/state/active-mode').write_text('multi-model', encoding='utf-8')
    recovery = root/'houmo/state/device-recovery/20260904T132351-fixture'; recovery.mkdir(parents=True, exist_ok=True)
    (recovery/'result.json').write_text(json.dumps({'status': 'failed', 'started_at': '2026-09-04T05:23:51Z', 'finished_at': '2026-09-04T05:24:10Z', 'mode': 'multi-model', 'secret': SECRET}), encoding='utf-8')
    (recovery/'first-fault.json').write_text(json.dumps({'line': 'ERR_FATAL token='+SECRET}), encoding='utf-8')
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == 'ps': output = '\n'.join(names.values())
        elif args[0] == 'inspect': output = json.dumps(raw)
        elif args[:2] == ('image', 'inspect'): output = json.dumps(images)
        elif args[0] == 'compose':
            path = Path(args[args.index('-f')+1]); output = json.dumps(saved[path.parent.name])
        elif args[0] == 'logs': output = '2026-09-07T00:05:00Z ERR_FATAL token='+SECRET if args[-1] == 'houmo-copaw' else ''
        else: raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, '')
    def probe(container, port):
        service = next(k for k,v in names.items() if '/'+v == container['Name'])
        spec = next(v for v in specs.values() if v.get('compose_service') == service)
        return {'slots': 1 if service == 'copaw' else spec['concurrency'], 'context': spec['context_size']}
    return {'run': run, 'probe': probe, 'paths': paths, 'calls': calls, 'raw': raw, 'saved': saved,
            'collect': lambda: collect(run, CONTAINERS, HEALTH_PORTS, paths, probe)}
