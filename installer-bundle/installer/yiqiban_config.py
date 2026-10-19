"""Deterministic configuration for sharing components with the local AI platform."""
from copy import deepcopy
import ipaddress
import json
from pathlib import Path
import re

REQUIRED_COMPONENTS = {'mysql', 'redis', 'paradedb'}
DATABASE_SERVICES = {'weknora-postgres', 'weknora-mysql', 'weknora-redis'}
DATABASE_VOLUMES = {'postgres-data', 'mysql-data', 'redis-data'}
GATEWAY_URL = 'http://host.docker.internal:17700/v1'
CHAT_MODELS = ('copaw-flash-9b', 'spark-x1-4b', 'qwen3.8-27b', 'qwen3-vl-8b', 'qwen3.6-35b-a3b')
MODES = dict(zip(CHAT_MODELS, ('copaw', 'spark-x1', 'qwen38', 'qwen-vl', 'qwen36')),
             **{'bge-m3': 'bge-m3', 'bge-reranker-v2-m3': 'reranker'})


def enabled(bundle):
    return bool(json.loads((Path(bundle) / 'release.json').read_text('utf-8')).get('yiqiban_resource_id'))


def validate_host(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', value):
        raise ValueError('翼起办访问地址需为 IPv4 或域名，不含协议、端口和路径')
    if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p) for p in value.split('.')):
        raise ValueError('翼起办访问地址无效')
    return value


def model_plan(selected):
    chats = [m for m in CHAT_MODELS if m in selected]
    if not chats:
        raise ValueError('翼起办需要至少一个对话模型：Copaw、Spark、Qwen3.8 或 Qwen3-VL')
    return {'chat': chats, 'default_chat': chats[0],
            'embedding': ['bge-m3'] if 'bge-m3' in selected else [],
            'rerank': ['bge-reranker-v2-m3'] if 'bge-reranker-v2-m3' in selected else []}


def model_files(plan):
    pro = []; workflow = []
    for kind, values in [('chat', plan['chat']), ('embedding', plan['embedding']), ('rerank', plan['rerank'])]:
        for index, name in enumerate(values):
            parameters = {'base_url': '${PLATFORM_MODEL_BASE_URL}', 'api_key': '${PLATFORM_MODEL_API_KEY}',
                          'provider': 'generic' if kind == 'rerank' else 'openai'}
            if kind == 'embedding':parameters['embedding_parameters'] = {'dimension': 1024, 'truncate_prompt_tokens': 0}
            pro.append({'id': 'platform-' + name, 'name': name,
                        'type': {'chat': 'KnowledgeQA', 'embedding': 'Embedding', 'rerank': 'Rerank'}[kind],
                        'source': 'remote', 'is_default': index == 0, 'parameters': parameters})
            workflow.append({'provider': 'openai_api_compatible', 'model': name,
                             'model_type': {'chat': 'llm', 'embedding': 'text-embedding', 'rerank': 'rerank'}[kind],
                             'credentials': {'api_key': '${PLATFORM_MODEL_API_KEY}', 'endpoint_url': '${PLATFORM_MODEL_BASE_URL}',
                                             'ModelName': name, 'mode': 'chat', 'context_size': '32768'}})
    paw = {'default_active': {'provider_id': 'ai-platform', 'model': plan['default_chat']}, 'providers': [
        {'id': 'ai-platform', 'base_url': '${PLATFORM_MODEL_BASE_URL}', 'api_key': '${PLATFORM_MODEL_API_KEY}',
         'extra_models': [{'id': name, 'name': name, 'supports_image': name.startswith(('qwen3.8', 'qwen3-vl', 'qwen3.6'))}
                          for name in plan['chat']]}]}
    # JSON is valid YAML and avoids a new privileged YAML writer dependency.
    return {'pro/builtin_models.yaml': {'builtin_models': pro},
            'agent-go/platform/builtin_models.yaml': {'builtin_models': workflow}, 'paw/builtin_models.yaml': paw}


def shared_compose(original, network, data):
    result = deepcopy(original)
    if len(result['services']) != 12 or not DATABASE_SERVICES.issubset(result['services']):
        raise ValueError('翼起办原编排与已适配版本不一致')
    for name in DATABASE_SERVICES:result['services'].pop(name)
    for name in DATABASE_VOLUMES:result['volumes'].pop(name)
    result['networks']['components'] = {'external': True, 'name': network}
    for name, row in result['services'].items():
        row['depends_on'] = {k: v for k, v in row.get('depends_on', {}).items() if k not in DATABASE_SERVICES}
        if name in {'weknora-backend', 'agent-go', 'agent-runner'}:row['networks'].append('components')
        if name in {'weknora-backend', 'agent-go', 'agent-runner', 'qwenpaw'}:
            row.setdefault('extra_hosts', []).append('host.docker.internal:host-gateway')
        row['pull_policy'] = 'never'
    for name in result['volumes']:
        if not re.fullmatch('[a-z0-9-]+', name):raise ValueError('翼起办数据卷名无效')
        result['volumes'][name] = {'driver': 'local', 'driver_opts': {'type': 'none', 'o': 'bind', 'device': str(Path(data) / name)}}
    return result


def connection_environment(existing, credentials, gateway_key, redis_password, gateway_ip):
    ipaddress.IPv4Address(gateway_ip)
    env = dict(existing)
    whitelist = [s.strip() for s in env.get('SSRF_WHITELIST_EXTRA', '').split(',') if s.strip()]
    env.update(DB_HOST='paradedb', DB_PORT='5432', DB_USER=credentials['postgres_user'], DB_NAME=credentials['postgres_db'],
               DB_PASSWORD=credentials['postgres_password'], REDIS_ADDR='redis:6379', REDIS_PASSWORD=redis_password,
               REDIS_DB=str(credentials['redis_pro_db']), REDIS_PREFIX='yiqiban:pro:',
               PLATFORM_MODEL_BASE_URL=GATEWAY_URL, PLATFORM_MODEL_API_KEY=gateway_key,
               SSRF_WHITELIST_EXTRA=','.join(dict.fromkeys([*whitelist, 'host.docker.internal', gateway_ip])))
    # The three removed database containers do not receive these generated values.
    for key in ['POSTGRES_PASSWORD', 'MYSQL_ROOT_PASSWORD', 'MYSQL_PASSWORD']:env.pop(key, None)
    return env


def bind_application_environment(compose, env):
    from components import escape_compose
    result = deepcopy(compose)
    for row in result['services'].values():
        if row.pop('env_file', None) is not None:
            row['environment'] = {**escape_compose(env), **row.get('environment', {})}
    # Only these values are used for Compose interpolation. Actual application
    # credentials use literal, dollar-escaped environment entries in a private file.
    public = {k: env[k] for k in ['WEB_PORT', 'WORKFLOW_PORT', 'PAW_PORT', 'XINGCHEN_WEB_URL',
                                   'QWENPAW_CONSOLE_URL', 'AGENT_SANDBOX_API_KEY']}
    return result, public


def sql_statements(text):
    """Split publisher SQL while respecting quoted comments and semicolons."""
    statements = []; current = []; quote = None; i = 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            if char == '\\' and i + 1 < len(text):i += 1; current.append(text[i])
            elif char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:i += 1; current.append(text[i])
                else:quote = None
        elif char in "'\"`":quote = char; current.append(char)
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:raise ValueError('未闭合 SQL 注释')
            if text.startswith('/*!', i):raise ValueError('不接受 SQL 可执行注释')
            current.append(' ');i = end + 1
        elif char == '#' or (text.startswith('--', i) and (i + 2 == len(text) or text[i + 2].isspace())):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end;current.append('\n')
        elif char == ';':
            if ''.join(current).strip():statements.append(''.join(current).strip())
            current = []
        else:current.append(char)
        i += 1
    if quote:raise ValueError('未闭合 SQL 引号')
    if ''.join(current).strip():statements.append(''.join(current).strip())
    return statements


def resumable_schema(text, expected=162):
    output = [];tables = []
    for statement in sql_statements(text):
        match = re.match(r'CREATE\s+TABLE\s+`([A-Za-z0-9_]+)`\s*\(', statement, re.I)
        if match:
            tables.append(match[1]);output.append(re.sub(r'^CREATE\s+TABLE', 'CREATE TABLE IF NOT EXISTS', statement, count=1, flags=re.I))
        elif re.fullmatch(r'SET\s+(?:NAMES\s+utf8mb4|FOREIGN_KEY_CHECKS\s*=\s*[01])', statement, re.I):output.append(statement)
        else:raise ValueError('翼起办初始化只允许当前业务库的建表语句和会话设置')
    if len(tables) != expected or len(set(tables)) != expected:raise ValueError('翼起办 SQL 表结构数量不符合清单')
    return ';\n'.join(output) + ';\n', tables


def read_env(path):
    values = {}
    for line in Path(path).read_text('utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1);values[key] = json.loads(value) if value.startswith('"') else value
    return values


def write_env(path, values):
    from common import atomic_text
    atomic_text(Path(path), ''.join(k + '=' + json.dumps(str(v), ensure_ascii=False) + '\n' for k, v in sorted(values.items())), 0o600)
