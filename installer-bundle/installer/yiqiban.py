"""Install YiQiBan after model acceptance, sharing the authenticated middleware."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys

from common import atomic_json, atomic_text, no_symlink_parents, read_json, sha256
from components import Components, data_owner
from port_takeover import Takeover
from yiqiban_config import (REQUIRED_COMPONENTS, bind_application_environment, connection_environment,
                             model_files, model_plan, read_env, resumable_schema, shared_compose,
                             validate_host, write_env)
from yiqiban_payload import load, saved_image_info
from storage import trusted_directory


class YiQiBan:
    def __init__(self, owner):
        self.owner = owner;self.flow = owner.workflow
        self.base = owner.storage.data_root / 'yiqiban';self.app = self.base / 'app'
        self.state_path = self.base / 'private.json';self.compose = self.app / 'compose.yaml'
        self.manifest = load(owner.bundle);self.source = owner.bundle / 'yiqiban/files'
        self.plan = model_plan(owner.selected)
        self.state = None

    def run(self, args, *, env=None, input=None, timeout=180, check=True):
        # Application credentials are deliberately absent from argv and diagnostics.
        result = subprocess.run([str(x) for x in args], env=env, input=input, text=True,
                                capture_output=True, timeout=timeout)
        if check and result.returncode:raise RuntimeError('翼起办操作失败：' + str(args[0]) + '，退出码 ' + str(result.returncode))
        return result

    def save(self):atomic_json(self.state_path, self.state, 0o600)

    def directory(self, path):
        trusted_directory(path, os.geteuid())
        path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def data_directory(self, path, owner):
        # UID 1001 services need writable mounted roots; their parent stays private.
        trusted_directory(path.parent, os.geteuid());no_symlink_parents(path, self.base)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != owner[0]:
                raise RuntimeError('翼起办数据卷归属不符')
        else:
            path.mkdir(mode=0o700);os.chown(path, *owner)

    def env(self, name, value):return dict(os.environ, **{name: value})

    def mysql(self, sql, *, application=False):
        user = self.state['mysql_user'] if application else 'root'
        password = self.state['mysql_password'] if application else self.flow.settings['components']['mysql_password']
        command = ['docker', 'exec', '-i', '--env', 'MYSQL_PWD', 'mysql', 'mysql', '--protocol=TCP', '-h127.0.0.1',
                   '-u' + user, '--batch', '--skip-column-names']
        if application:command += ['-D', self.state['mysql_db']]
        return self.run(command, input=sql, env=self.env('MYSQL_PWD', password), timeout=180).stdout.strip()

    def postgres(self, sql, *, application=False):
        user = self.state['postgres_user'] if application else 'paradedb'
        database = self.state['postgres_db'] if application else 'paradedb'
        password = self.state['postgres_password'] if application else self.flow.settings['components']['paradedb_password']
        return self.run(['docker', 'exec', '-i', '--env', 'PGPASSWORD', 'paradedb', 'psql', '-h127.0.0.1', '-U', user,
                         '-d', database, '-v', 'ON_ERROR_STOP=1', '-Atq'], input=sql,
                        env=self.env('PGPASSWORD', password)).stdout.strip()

    def redis(self, *args):
        return self.run(['docker', 'exec', '--env', 'REDISCLI_AUTH', 'redis', 'redis-cli', '--no-auth-warning', '--raw', *args],
                        env=self.env('REDISCLI_AUTH', self.flow.settings['components']['redis_password'])).stdout.strip()

    def initialize(self):
        if not REQUIRED_COMPONENTS.issubset(self.flow.settings['components']['selected']):
            raise RuntimeError('翼起办必需 MySQL、Redis、ParadeDB，请返回组件选择页面')
        Components(self.flow).acceptance()
        self.directory(self.base)
        if self.state_path.exists() or self.state_path.is_symlink():
            if (self.state_path.is_symlink() or not self.state_path.is_file()
                    or self.state_path.stat().st_uid != os.geteuid() or self.state_path.stat().st_mode & 0o077):
                raise RuntimeError('翼起办状态文件必须由管理员拥有且仅管理员可读写')
        self.state = read_json(self.state_path, {})
        fingerprint = hashlib.sha256(json.dumps({k: self.flow.settings['components'][k] for k in
                         ['mysql_password', 'redis_password', 'paradedb_password']}, sort_keys=True).encode()).hexdigest()
        if self.state:
            if self.state.get('component_fingerprint') != fingerprint:raise RuntimeError('共享组件凭据已改变，不能重新初始化翼起办旧数据')
            if self.state.get('resource_id') != self.manifest['release_id']:raise RuntimeError('已有不同翼起办版本，需要单独的迁移流程')
            self.reserve_redis();return
        if any(self.base.iterdir()):raise RuntimeError('翼起办目录已有未登记的数据，不覆盖')
        token = secrets.token_hex(5)
        requested = read_json(self.owner.root / 'private/request.json').get('yiqiban',{}).get('public_host')
        if requested:host=validate_host(requested)
        else:
            default_routes = json.loads(self.run(['ip', '-j', 'route', 'get', '1.1.1.1']).stdout)
            host = validate_host(default_routes[0]['prefsrc'])
        keyspace = self.redis('INFO', 'keyspace')
        occupied = {int(line.split(':')[0][2:]) for line in keyspace.splitlines() if line.startswith('db') and ':' in line}
        count = int(self.redis('CONFIG', 'GET', 'databases').splitlines()[-1])
        available = [n for n in range(count - 1, 0, -1) if n not in occupied]
        if len(available) < 2:raise RuntimeError('共享 Redis 没有两个空闲逻辑库，不清理已有数据')
        self.state = {'schema': 1, 'resource_id': self.manifest['release_id'], 'component_fingerprint': fingerprint,
                      'postgres_user': 'yiqiban_pro_' + token, 'postgres_db': 'yiqiban_pro_' + token,
                      'postgres_password': secrets.token_hex(24), 'mysql_user': 'yiqiban_' + token,
                      'mysql_db': 'yiqiban_agent_' + token, 'mysql_password': secrets.token_hex(24),
                      'redis_pro_db': available[0], 'redis_workflow_db': available[1], 'host': host,
                      'managed': {}, 'status': 'prepared'}
        self.save()
        self.reserve_redis()

    def reserve_redis(self):
        identity=self.state['mysql_db']
        script="if redis.call('DBSIZE') == 0 or redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[1], ARGV[1]); return 1 else return 0 end"
        for database in [self.state['redis_pro_db'],self.state['redis_workflow_db']]:
            if self.redis('-n',str(database),'EVAL',script,'1','__ai_platform_yiqiban_reservation',identity)!='1':
                raise RuntimeError('翼起办 Redis 逻辑库已被其他应用占用，不清理或覆盖')

    def databases(self):
        s = self.state;database = s['mysql_db'];user = s['mysql_user']
        self.mysql(f'CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n'
                   f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{s['mysql_password']}';\n"
                   f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{user}'@'%';\n")
        schema, tables = resumable_schema((self.source / 'sql/01-agent-schema.sql').read_text('utf-8-sig'))
        self.mysql(schema, application=True)
        actual = set(self.mysql('SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE();', application=True).splitlines())
        if not set(tables).issubset(actual):raise RuntimeError('翼起办 MySQL 表结构初始化不完整')
        role = s['postgres_user'];database = s['postgres_db']
        self.postgres(f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN "
                      f"CREATE ROLE {role} LOGIN PASSWORD '{s['postgres_password']}' NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END$$;")
        if self.postgres(f"SELECT 1 FROM pg_database WHERE datname='{database}';") != '1':
            self.postgres(f'CREATE DATABASE {database} OWNER {role};')
        # Install privileged extensions with the existing component administrator,
        # then run the application with only its dedicated database owner.
        password = self.flow.settings['components']['paradedb_password']
        self.run(['docker', 'exec', '-i', '--env', 'PGPASSWORD', 'paradedb', 'psql', '-h127.0.0.1', '-U', 'paradedb',
                  '-d', database, '-v', 'ON_ERROR_STOP=1', '-Atq'], env=self.env('PGPASSWORD', password),
                 input='CREATE EXTENSION IF NOT EXISTS vector;\nCREATE EXTENSION IF NOT EXISTS pg_search;\n')
        if self.postgres('SELECT 1;', application=True) != '1':raise RuntimeError('翼起办 PostgreSQL 业务账号认证失败')
        self.state['databases_ready'] = True;self.save()
        self.owner.event('翼起办已复用共享组件，独立业务库和账号验证通过；原有数据库未清空')

    def managed_text(self, path, content, mode=0o644):
        no_symlink_parents(path, self.base)
        key = path.relative_to(self.base).as_posix()
        expected = self.state['managed'].get(key)
        if path.is_symlink():raise RuntimeError('翼起办配置不能是链接')
        desired = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if path.exists() and expected and sha256(path) not in {expected, desired}:raise RuntimeError('翼起办托管配置已被修改，不覆盖：' + key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, content, mode)
        self.state['managed'][key] = sha256(path);self.save()

    def images(self):
        self.image_owners = {}
        for row in self.manifest['images']:
            self.owner.pause_point();self.owner.update('准备翼起办镜像：' + row['service'], force=True)
            provenance = saved_image_info(self.source / row['archive'], row['tag'])
            result = self.run(['docker', 'image', 'inspect', row['tag']], check=False)
            if result.returncode:
                self.run(['docker', 'load', '-i', self.source / row['archive']], timeout=900)
                result = self.run(['docker', 'image', 'inspect', row['tag']])
            info = json.loads(result.stdout)[0]
            if (info['Id'] not in {row['id'], provenance['config_id']} or info['Architecture'] != 'arm64' or info['Os'] != 'linux'
                    or info['RootFS']['Layers'] != provenance['rootfs_diff_ids']):
                raise RuntimeError('翼起办镜像身份不符：' + row['service'])
            if row['service'] in {'agent-go', 'agent-runner'}:
                self.image_owners[row['service']] = data_owner(self, info)

    def configure(self):
        self.directory(self.app)
        for row in self.manifest['files']:
            relative = row['path']
            if relative.endswith('.tar') or relative == 'compose.yaml':continue
            target = self.app / relative;no_symlink_parents(target, self.app)
            if target.exists():
                if target.is_symlink() or sha256(target) != row['sha256']:raise RuntimeError('翼起办模板内容冲突：' + relative)
            else:
                target.parent.mkdir(parents=True, exist_ok=True);shutil.copyfile(self.source / relative, target);target.chmod(0o644)
        if not (self.app / '.env').exists() and not self.state.get('application_env'):
            self.run([sys.executable, self.app / 'configure.py', '--host', self.state['host']], timeout=180)
        current = self.state.get('application_env') or read_env(self.app / '.env')
        key = read_env(self.owner.home / 'houmo-docker-deployment/.env')['HOUMO_API_KEY']
        networks = json.loads(self.run(['docker', 'network', 'inspect', 'ai-platform-components_default']).stdout)
        if networks[0].get('Labels', {}).get('com.docker.compose.project') != 'ai-platform-components':raise RuntimeError('共享组件网络归属不符')
        bridge = json.loads(self.run(['docker', 'network', 'inspect', 'bridge']).stdout)
        gateway_ip = bridge[0]['IPAM']['Config'][0]['Gateway']
        env = connection_environment(current, self.state, key, self.flow.settings['components']['redis_password'], gateway_ip)
        self.state['application_env'] = env;self.save()
        for path, data in model_files(self.plan).items():
            self.managed_text(self.app / 'runtime' / path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        for service in ['agent-go', 'agent-runner']:
            database = 'DBType = "MYSQL"\nHost = "mysql"\nPort = 3306\nUser = ' + json.dumps(self.state['mysql_user'])
            database += '\nPassword = ' + json.dumps(self.state['mysql_password']) + '\nDBName = ' + json.dumps(self.state['mysql_db']) + '\n'
            self.managed_text(self.app / 'runtime' / service / 'db/db.toml', database)
            redis = 'Addr = "redis:6379"\nPassword = ' + json.dumps(self.flow.settings['components']['redis_password'])
            redis += '\nDB = ' + str(self.state['redis_workflow_db']) + '\nMaxRetries = 3\nIsCluster = false\n'
            self.managed_text(self.app / 'runtime' / service / 'redis/redis.toml', redis)
        original = read_json(self.source / 'compose.yaml')
        data_root = self.base / 'volumes';self.directory(data_root)
        compose = shared_compose(original, 'ai-platform-components_default', data_root)
        compose, interpolation = bind_application_environment(compose, env)
        write_env(self.app / '.env', interpolation)
        for name in compose['volumes']:
            service={'runner-storage':'agent-runner','agent-storage':'agent-go'}.get(name)
            self.data_directory(data_root / name, self.image_owners[service] if service else (0, 0))
        self.managed_text(self.compose, json.dumps(compose, ensure_ascii=False, indent=2) + '\n', 0o600)
        self.state['status'] = 'configured';self.save()

    def docker_compose(self, *args, timeout=900):
        return self.run(['docker', 'compose', '--project-directory', self.app, '--env-file', self.app / '.env', '-f', self.compose, *args], timeout=timeout)

    def recover_deployment(self, transaction):
        baseline = transaction.record.get('baseline_containers', [])
        for ident in self.docker_compose('ps', '-aq').stdout.split():
            info = json.loads(self.run(['docker', 'inspect', ident]).stdout)[0]
            labels = info['Config'].get('Labels') or {}
            if (ident not in baseline and labels.get('com.docker.compose.project.working_dir') == str(self.app)
                    and labels.get('ai-platform.installer.task') == transaction.state['token']
                    and labels.get('ai-platform.installer.attempt') == transaction.record['attempt']):
                transaction.created(info)

    def deploy(self):
        load(self.owner.bundle, verify_files=True);self.initialize();self.images();self.databases();self.configure()
        transaction = Takeover(self.flow, 'yiqiban-ports')
        if transaction.record and transaction.record['status'] not in ('committed', 'rolled-back'):
            self.recover_deployment(transaction);transaction.rollback()
        transaction.begin()
        ids = self.docker_compose('ps', '-aq').stdout.split()
        transaction.record['baseline_containers'] = ids;transaction.save()
        spec = read_json(self.compose)
        for row in spec['services'].values():
            row.setdefault('labels', {}).update({'ai-platform.installer.task': transaction.state['token'],
                                                 'ai-platform.installer.attempt': transaction.record['attempt']})
        self.managed_text(self.compose, json.dumps(spec, ensure_ascii=False, indent=2) + '\n', 0o600)
        try:
            occupied = set()
            for ident in ids:
                info = json.loads(self.run(['docker', 'inspect', ident]).stdout)[0]
                if (info['Config'].get('Labels') or {}).get('com.docker.compose.project.working_dir') != str(self.app):
                    raise RuntimeError('翼起办已有容器归属不符')
                if info['State']['Running']:
                    occupied.update(int(p['HostPort']) for rows in (info['HostConfig'].get('PortBindings') or {}).values() for p in rows or [])
            transaction.claim({8081, 3000, 8088} - occupied, owned=ids)
            transaction.phase('deploying')
            self.docker_compose('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '900', timeout=960)
            self.recover_deployment(transaction)
            transaction.commit()
        except Exception:
            # Only this deployment's containers can be removed; shared databases are excluded.
            self.recover_deployment(transaction)
            transaction.rollback();raise
        self.state['status'] = 'deployed';self.save()
        self.owner.state['yiqiban_urls'] = {'portal': 'http://' + self.state['host'] + ':8081',
                                           'workflow': 'http://' + self.state['host'] + ':3000',
                                           'paw': 'http://' + self.state['host'] + ':8088/chat'}
        self.owner.event('翼起办应用服务已就绪，复用 MySQL、Redis、ParadeDB；等待模型联通验收')

    def check(self):
        from yiqiban_verify import verify
        self.initialize();report = verify(self)
        self.state['status'] = 'verified';self.save()
        self.owner.state['yiqiban_acceptance'] = report
        env = self.state['application_env']
        export = self.owner.home / 'AI-Platform-1.3-YiQiBan-connections.json'
        no_symlink_parents(export, self.owner.home)
        if export.is_symlink():raise RuntimeError('翼起办连接配置导出路径不能是链接')
        atomic_json(export, dict(self.owner.state['yiqiban_urls'], paw_username=env['QWENPAW_AUTH_USERNAME'],
                                paw_password=env['QWENPAW_AUTH_PASSWORD'], business_activation='首次注册管理员并导入有效许可证'), 0o600)
        account = pwd.getpwnam('cix');os.chown(export, account.pw_uid, account.pw_gid)
        self.owner.event('翼起办模型接入与真实推理联通测试通过；业务授权待首次管理员注册和许可证导入')
