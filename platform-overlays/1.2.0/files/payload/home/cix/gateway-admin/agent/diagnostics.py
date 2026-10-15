"""Read-only, bounded, allow-listed runtime inspection. Never export raw Docker data."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

from .configuration import ALL_BINDINGS, CONFIG_SCHEMA, RuntimeSettings

BASELINE = Path(__file__).with_name('runtime_baseline.json')
MODEL_COMPONENTS = {'gateway': 'ai-model-gateway', 'admin': 'modeldeck', 'agent': 'modeldeck-agent',
                    'cv': 'cix-cv', 'sensevoice': 'sensevoice-cpu',
                    'matcha': 'matcha-tts-cpu', 'kokoro': 'kokoro-cix'}
FAULT_PATTERN = re.compile(r'ERR_FATAL|SyncYieldErr|synchronize timeout|out of memory|CUDA out of memory', re.I)
SAFE_IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,250}$')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def bounded_command(command, timeout=4, max_bytes=2_000_000):
    """Avoid unbounded log lines / child output exhausting the restricted agent."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    body = bytearray()
    exceeded = threading.Event()
    def read():
        while chunk := process.stdout.read(8192):
            remaining = max_bytes - len(body)
            body.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded.set(); process.kill(); break
    reader = threading.Thread(target=read, daemon=True); reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(); reader.join(timeout=2)
        process.stdout.close()
        return subprocess.CompletedProcess(command, 124, '', '')
    reader.join(timeout=2)
    process.stdout.close()
    return subprocess.CompletedProcess(command, 125 if exceeded.is_set() else process.returncode, body.decode('utf-8', errors='replace'), '')


def number(value):
    if isinstance(value, bool): return None
    try:
        value = float(value)
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else None
    except (TypeError, ValueError): return None


def arguments(command):
    # Shell scripts are not expanded or executed. Only direct numeric flags count.
    if not isinstance(command, list): return {}
    if command and str(command[0]).rsplit('/', 1)[-1] in {'sh', 'bash', 'dash', 'cmd', 'powershell'}: return {}
    flags = {'-np': 'slots', '--parallel': 'slots', '-c': 'context', '--ctx-size': 'context'}
    result = {}
    for index, arg in enumerate(command):
        if not isinstance(arg, str): continue
        key, separator, value = arg.partition('=')
        if key in flags:
            if not separator: value = command[index + 1] if index + 1 < len(command) else None
            result[flags[key]] = number(value)
    return result


def environment(value):
    if isinstance(value, dict): return value
    return dict(item.split('=', 1) for item in (value or []) if isinstance(item, str) and '=' in item)


def setting_value(binding, env):
    value = env.get(binding.env_key)
    if value is None: return None
    try:
        value = str(value)
        if isinstance(RuntimeSettings.model_fields[binding.setting].default, bool):
            if value.lower() not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}: return None
        decoded = binding.decode(value)
        if isinstance(decoded, float) and not math.isfinite(decoded): return None
        return decoded if isinstance(decoded, (int, float, bool)) else None
    except (TypeError, ValueError, OverflowError): return None


def compare(actual, expected):
    return 'unknown' if actual is None or expected is None else 'same' if actual == expected else 'different'


def image_name(value):
    return value if isinstance(value, str) and SAFE_IMAGE.fullmatch(value) else None


def props(container, port):
    """Read model settings only; never request prompts, slots contents or inference."""
    networks = container.get('NetworkSettings', {}).get('Networks', {})
    address = next((v.get('IPAddress') for v in networks.values() if v.get('IPAddress')), None)
    if not address or not ipaddress.ip_address(address).is_private: return None
    key = environment(container.get('Config', {}).get('Env')).get('HOUMO_API_KEY', '')
    request = Request(f'http://{address}:{int(port)}/props', headers={'Authorization': 'Bearer ' + key})
    # No redirects to other hosts and no inherited HTTP proxy for private model endpoints.
    from urllib.request import HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=3) as response:
        data = response.read(262145)
        if len(data) > 262144: return None
    data = json.loads(data)
    if not isinstance(data, dict): return None
    generation = data.get('default_generation_settings')
    if not isinstance(generation, dict): generation = {}
    return {'slots': number(data.get('total_slots')), 'context': number(generation.get('n_ctx'))}


def recovery_records(deploy):
    root = deploy / 'state/device-recovery'; records = []
    if not root.is_dir(): return records
    for folder in sorted(root.iterdir(), reverse=True)[:30]:
        if not folder.is_dir() or folder.is_symlink(): continue
        try:
            path = folder / 'result.json'
            if path.is_symlink() or path.stat().st_size > 262144: continue
            result = json.loads(path.read_text(encoding='utf-8'))
            fault_path = folder / 'first-fault.json'
            fault = json.loads(fault_path.read_text(encoding='utf-8')) if fault_path.is_file() and not fault_path.is_symlink() and fault_path.stat().st_size < 262144 else {}
            codes = sorted(set(m.group().lower() for m in FAULT_PATTERN.finditer(str(fault.get('line', '')))))
            records.append({'id': folder.name, 'status': result.get('status') if result.get('status') in {'succeeded', 'failed'} else 'unknown',
                            'started_at': result.get('started_at'), 'finished_at': result.get('finished_at'),
                            'mode': result.get('mode') if result.get('mode') in {'copaw', 'spark-x1', 'qwen38', 'qwen36', 'qwen-vl', 'glm-ocr', 'bge-m3', 'reranker'} else 'unknown',
                            'fault_codes': codes, 'source': 'device-recovery/result.json'})
        except (OSError, ValueError, TypeError): continue
    return records


def collect(run, containers, ports, paths, probe=props):
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    targets = {**containers, 'admin': 'gateway-admin', 'agent': 'gateway-admin-agent'}
    errors = []; collected_at = now()
    def docker_json(*args):
        try:
            response = run(*args)
            if response.returncode: return None
            return json.loads(response.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired): return None
    listed = run('ps', '-a', '--format', '{{.Names}}')
    inventory_ok = listed.returncode == 0
    existing = set(listed.stdout.splitlines()) & set(targets.values()) if inventory_ok else set()
    inspected = docker_json('inspect', *sorted(existing)) if existing else []
    inventory_ok = inventory_ok and isinstance(inspected, list)
    raw = {i['Name'].lstrip('/'): i for i in inspected or []} if inventory_ok else {}
    if not inventory_ok: errors.append('容器清单读取失败，实际运行值未知')
    ids = sorted({i['Image'] for i in raw.values()})
    images = docker_json('image', 'inspect', *ids) if ids else []
    image_info = {i['Id']: i for i in images or []}

    def saved_config(pair):
        group, path = pair
        data = docker_json('compose', '--project-directory', str(path.parent), '-f', str(path), '--profile', '*', 'config', '--format', 'json')
        try: digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError: digest = None
        return group, {'services': data.get('services', {}) if isinstance(data, dict) else {}, 'available': isinstance(data, dict), 'sha256': digest}
    with ThreadPoolExecutor(max_workers=3) as pool:
        saved = dict(pool.map(saved_config, ((k, paths[k]) for k in ('gateway', 'houmo', 'sensevoice'))))
    for group, data in saved.items():
        if not data['available']: errors.append(f'{group} 保存配置无法解析')
    saved_services = {k: v for group in saved.values() for k, v in group['services'].items()}
    components = baseline['components']; specs = baseline['models']
    npu = {v['compose_service']: {'id': k, **v} for k, v in specs.items() if v['service_component'].startswith('hliellama') and v.get('gateway_enabled')}
    rows = []
    for service, name in targets.items():
        item = raw.get(name); config = item.get('Config', {}) if item else {}; state = item.get('State', {}) if item else {}
        saved_item = saved_services.get(service)
        component = MODEL_COMPONENTS.get(service) or (npu.get(service) or {}).get('service_component')
        expected_image = components.get(component, {}).get('image')
        row = {'service': service, 'name': name, 'container_id': item.get('Id') if item else None,
               'state': state.get('Status') if item else 'absent' if inventory_ok else 'unknown',
               'health': state.get('Health', {}).get('Status') if state.get('Running') else None, 'oom_killed': state.get('OOMKilled') if item else None,
               'exit_code': state.get('ExitCode') if item else None, 'restart_count': item.get('RestartCount') if item else None,
               'created_at': item.get('Created') if item else None, 'started_at': state.get('StartedAt'), 'finished_at': state.get('FinishedAt'),
               'image': image_name(config.get('Image')), 'image_id': item.get('Image') if item else None,
               'repo_digests': [image_name(v) for v in image_info.get(item.get('Image'), {}).get('RepoDigests', []) if image_name(v)] if item else [],
               'saved_image': image_name(saved_item.get('image')) if saved_item else None, 'baseline_image': expected_image,
               'startup': arguments(config.get('Cmd')), 'saved_startup': arguments(saved_item.get('command')) if saved_item else {},
               'memory_limit_bytes': item.get('HostConfig', {}).get('Memory') if item else None,
               'cpu_limit': (item.get('HostConfig', {}).get('NanoCpus', 0) / 1e9) if item else None,
               'read_only': item.get('HostConfig', {}).get('ReadonlyRootfs') if item else None}
        row['saved_image_diff'] = compare(row['image'], row['saved_image']); row['baseline_image_diff'] = compare(row['image'], expected_image)
        rows.append(row)

    parameters = []
    def model_parameters(pair):
        service, spec = pair; row = next(r for r in rows if r['service'] == service)
        actual = None
        if row['state'] == 'running':
            try: actual = probe(raw[row['name']], ports[service])
            except (OSError, ValueError, TypeError, KeyError): pass
        return {'service': service, 'model': spec['id'], 'state': row['state'], 'source': 'service_props' if actual else 'not_reported',
                'actual_slots': (actual or {}).get('slots'), 'actual_context': (actual or {}).get('context'),
                'startup_slots': row['startup'].get('slots'), 'startup_context': row['startup'].get('context'),
                'saved_slots': row['saved_startup'].get('slots'), 'saved_context': row['saved_startup'].get('context'),
                'saved_available': service in saved_services, 'baseline_slots': spec.get('concurrency'), 'baseline_context': spec.get('context_size'),
                'slots_baseline_diff': compare((actual or {}).get('slots'), spec.get('concurrency')),
                'context_baseline_diff': compare((actual or {}).get('context'), spec.get('context_size')),
                'slots_saved_diff': compare(row['startup'].get('slots'), row['saved_startup'].get('slots')),
                'context_saved_diff': compare(row['startup'].get('context'), row['saved_startup'].get('context'))}
    with ThreadPoolExecutor(max_workers=4) as pool: parameters = list(pool.map(model_parameters, npu.items()))
    settings = []
    defaults = RuntimeSettings().model_dump()
    labels = {field['id']: field for group in CONFIG_SCHEMA['groups'] for field in group['fields']}
    for binding in ALL_BINDINGS:
        service = binding.service
        container = raw.get(targets[service], {})
        actual = setting_value(binding, environment(container.get('Config', {}).get('Env')))
        saved_value = setting_value(binding, environment(saved_services.get(service, {}).get('environment')))
        settings.append({'id': binding.setting, 'env_key': binding.env_key, 'service': service, 'actual': actual,
                         'label': labels[binding.setting]['label'], 'unit': labels[binding.setting].get('unit', ''),
                         'saved': saved_value, 'default': defaults[binding.setting], 'diff': compare(actual, saved_value),
                         'source': 'container_env' if actual is not None else 'not_set', 'active': container.get('State', {}).get('Running', False)})

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
    def log_faults(row):
        if not row['container_id']: return {'service': row['service'], 'available': False, 'events': []}
        result = run('logs', '--since', since, '--tail', '200', '--timestamps', row['name'], timeout=5, max_bytes=262144)
        events = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                for match in FAULT_PATTERN.finditer(line):
                    timestamp = line.split(' ', 1)[0]
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z', timestamp): timestamp = collected_at
                    events.append({'id': hashlib.sha256((row['container_id'] + line + match.group().lower()).encode()).hexdigest(),
                                   'service': row['service'], 'at': timestamp, 'code': match.group().lower(), 'source': 'container_log'})
        return {'service': row['service'], 'available': result.returncode == 0, 'events': events[-30:]}
    with ThreadPoolExecutor(max_workers=4) as pool: log_scans = list(pool.map(log_faults, rows))
    faults = [event for scan in log_scans for event in scan['events']]
    for row in rows:
        for code, condition in [('oom_killed', row['oom_killed']), ('unhealthy', row['health'] == 'unhealthy')]:
            if condition:
                faults.append({'id': hashlib.sha256((str(row['container_id']) + str(row['started_at']) + code).encode()).hexdigest(),
                               'service': row['service'], 'at': row['finished_at'] if code == 'oom_killed' and row['finished_at'] and not row['finished_at'].startswith('0001-') else collected_at, 'code': code, 'source': 'container_state'})
    try: mode = (paths['houmo'].parent / 'state/active-mode').read_text(encoding='utf-8').strip()
    except OSError: mode = 'unknown'
    return {'collected_at': collected_at, 'inventory_available': inventory_ok, 'errors': errors,
            'baseline': baseline, 'configured_mode': mode if mode in {'copaw', 'spark-x1', 'qwen38', 'qwen36', 'qwen-vl', 'glm-ocr', 'bge-m3', 'reranker'} else 'unknown',
            'containers': rows, 'parameters': parameters, 'settings': settings,
            'configuration_files': [{'group': k, 'available': v['available'], 'sha256': v['sha256']} for k, v in saved.items()],
            'faults': faults, 'fault_scan': {'since': since, 'tail_per_container': 200, 'services': [{k: v for k, v in s.items() if k != 'events'} for s in log_scans]},
            'recoveries': recovery_records(paths['houmo'].parent)}
