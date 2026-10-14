"""Model installation transactions; no arbitrary paths or shell commands."""
from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from pathlib import Path

from .model_store import CHUNK, atomic_json, digest, file_lock, now, under


class InstallFailure(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class Installer:
    def __init__(self, store, home, runtime):
        self.store, self.home, self.runtime = store, Path(home), runtime
        self.catalog = store.catalog
        self.deploy = self.home / 'houmo-docker-deployment'
        self.state_path = self.deploy / 'state/model-installations.json'

    def mode(self):
        path = self.deploy / 'state/active-mode'
        return path.read_text().strip() if path.exists() else 'copaw'

    def files_present(self, model):
        try:
            return all(under(self.home, f['target']).stat().st_size == f['size'] for f in model['files'])
        except OSError:
            return False

    def state(self):
        if self.state_path.exists():
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('models'), dict)
                    or not all(isinstance(row, dict) and isinstance(row.get('enabled', False), bool)
                               and isinstance(row.get('installed', False), bool) for row in value['models'].values())):
                raise ValueError('安装状态损坏，请核查备份；未覆盖现有状态')
            return value
        # First use adopts existing deployments without enabling absent models.
        values, containers = {}, {}
        registry = self.home / 'ai-model-gateway/config/models.json'
        policy = {m['id']: m.get('enabled', True) for m in json.loads(registry.read_text(encoding='utf-8'))['models']} if registry.exists() else {}
        for model in self.catalog.models.values():
            present = self.files_present(model)
            if not present and not model['capability'].startswith('cv_'):
                try:
                    if model['container'] not in containers:
                        containers[model['container']] = self.runtime.inspect(model)
                    info = containers[model['container']]
                    present = bool(info and info['State'].get('Running'))
                except (OSError, RuntimeError):
                    pass
            if present:
                values[model['id']] = {'installed': True, 'enabled': policy.get(model['id'], True), 'status': 'legacy',
                                       'service': model['service'], 'version': model['version']}
        return {'schema_version': 1, 'revision': 0, 'models': values}

    def save(self, state):
        state['revision'] = state.get('revision', 0) + 1
        state['updated_at'] = now()
        atomic_json(self.state_path, state)

    def snapshot(self):
        state, mode = self.state(), self.mode()
        rows, images, containers = [], {}, {}
        for model in self.catalog.models.values():
            record = state['models'].get(model['id'], {})
            if model['image'] not in images:
                try:
                    images[model['image']] = self.runtime.image_ready(model)
                except (OSError, ValueError, RuntimeError):
                    images[model['image']] = False
            if model['container'] not in containers:
                try:
                    item = self.runtime.inspect(model)
                    containers[model['container']] = bool(item and item['State'].get('Running'))
                except (OSError, RuntimeError):
                    containers[model['container']] = False
            cached = sum(f['size'] for f in model['files'] if self.store.ready(f))
            rows.append({k: model[k] for k in ('id', 'capability', 'device', 'version', 'modes', 'image', 'download_bytes', 'min_ram_gb')} | {
                'file_count': len(model['files']), 'cached_bytes': cached,
                'downloaded': cached == model['download_bytes'], 'installed': bool(record.get('installed')),
                'enabled': bool(record.get('enabled')), 'status': record.get('status', 'not_installed'),
                'running': containers[model['container']], 'mode_compatible': not model['modes'] or mode in model['modes'],
                'image_ready': images[model['image']], 'last_probe': record.get('last_probe'),
                'rollback_available': (self.store.root / 'transactions' / (model['id'] + '.json')).exists(),
            })
        return {'models': rows, 'mode': mode, 'managed': self.state_path.exists(),
                'storage_free_bytes': shutil.disk_usage(self.store.root).free, 'images': images,
                'offline_inbox': str(self.store.root / 'inbox')}

    def plan(self, ids, activate=True):
        models = self.catalog.select(ids)
        errors, notices = self.runtime.preflight(models, activate)
        state = self.state()
        for model in models:
            if state['models'].get(model['id'], {}).get('status') in {'installing', 'recovery_required'}:
                errors.append(model['id'] + ' 有未完成的安装事务，请先恢复旧文件')
            if not activate:
                item = self.runtime.inspect(model)
                if item and item['State'].get('Running'):
                    errors.append(model['id'] + ' 的服务正在运行；请选择安装并启用，或先停用共享该服务的全部模型')
        download = self.store.remaining(ids)
        installed = sum(m['download_bytes'] for m in models)
        # Includes cache, staging/backup and a bounded working reserve.
        required = download + installed + 256 * CHUNK
        if shutil.disk_usage(self.store.root).free < required:
            errors.append('模型存储空间不足：下载、安装备份需要 ' + str(round(required / 1024 ** 3, 2)) + ' GiB')
        if shutil.disk_usage(self.home).free < installed + 256 * CHUNK:
            errors.append('模型安装目标磁盘空间不足')
        return {'valid': not errors, 'models': ids, 'activate': activate, 'download_bytes': download,
                'required_free_bytes': required, 'errors': list(dict.fromkeys(errors)), 'notices': notices,
                'mode': self.mode(), 'revision': state.get('revision', 0),
                'steps': ['校验文件 / 复用缓存', '准备运行环境', '保存回退文件', '安装权重'] +
                         (['启动所选服务', '逐模型实际推理自测', '开放网关调用'] if activate else ['保存为已安装、未启用'])}

    def prepare(self, ids, progress):
        for model in self.catalog.select(ids):
            for item in model['files']:
                if self.store.ready(item):
                    continue
                target = under(self.home, item['target'])
                if target.is_file() and target.stat().st_size == item['size']:
                    progress({'stage': '核对已有模型文件', 'file': item['target']})
                    if digest(target) == item['sha256']:
                        cache = self.store.cached(item)
                        self.copy(target, cache)
                        self.store.receipt(item)
                        continue
                self.store.download(item, progress)
        return {'models': ids, 'status': 'downloaded'}

    @staticmethod
    def copy(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    def peers(self, state, model):
        return [m for m in self.catalog.models.values() if m['container'] == model['container']
                and state['models'].get(m['id'], {}).get('enabled')]

    def install(self, ids, activate, progress):
        plan = self.plan(ids, activate)
        if not plan['valid']:
            raise ValueError('；'.join(plan['errors']))
        self.prepare(ids, progress)
        completed = []
        with file_lock(self.deploy / 'state/modelctl.lock'):
            # Recheck after download, since an operator may have switched mode.
            current = self.plan(ids, activate)
            if not current['valid']:
                raise ValueError('；'.join(current['errors']))
            for model in self.catalog.select(ids):
                try:
                    self.install_one(model, activate, progress)
                    completed.append(model['id'])
                except Exception as exc:
                    raise InstallFailure(str(exc), {'completed_models': completed, 'failed_model': model['id']}) from exc
        return {'models': completed, 'activated': activate, 'message': '安装及自测完成' if activate else '模型已安装，尚未启用'}

    def install_one(self, model, activate, progress):
        state = self.state()
        previous = copy.deepcopy(state)
        peers = self.peers(state, model)
        transaction = {'schema_version': 1, 'id': uuid.uuid4().hex, 'model': model['id'], 'status': 'preparing',
                       'previous_state': previous, 'files': [], 'created_at': now()}
        journal = self.store.root / 'transactions' / (model['id'] + '.json')
        backup_root = self.store.root / 'backups' / transaction['id']
        # All cache inputs are verified before altering a live destination.
        for item in model['files']:
            self.store.verify(self.store.cached(item), item)
        for peer in peers:
            if peer['id'] != model['id']:
                for item in peer['files']:
                    self.store.verify(under(self.home, item['target']), item)
        progress({'stage': '保存安装事务与旧文件', 'model': model['id']})
        for item in model['files']:
            target = under(self.home, item['target'])
            backup = under(backup_root, item['target'])
            existed = target.exists()
            if existed:
                self.copy(target, backup)
            transaction['files'].append({'target': item['target'], 'existed': existed})
        atomic_json(journal, transaction)
        for peer in peers:
            state['models'][peer['id']]['enabled'] = False
        state['models'][model['id']] = {'installed': False, 'enabled': False, 'status': 'installing',
                                      'service': model['service'], 'version': model['version']}
        self.save(state)
        transaction['status'] = 'applying'; atomic_json(journal, transaction)
        stopped = False
        try:
            self.runtime.stop(model); stopped = True
            for item in model['files']:
                target = under(self.home, item['target'])
                staging = target.with_name(target.name + '.install-' + transaction['id'])
                self.copy(self.store.cached(item), staging)
                staging.replace(target)
            state['models'][model['id']].update(installed=True, status='installed')
            if activate:
                self.runtime.start(model, progress)
                # Shared CV peers were temporarily taken out of admission, so verify each.
                probes = {m['id']: m for m in [*peers, model]}
                for peer in probes.values():
                    progress({'stage': '实际推理自测：' + peer['id'], 'model': peer['id']})
                    result = self.runtime.probe(peer)
                    state['models'].setdefault(peer['id'], {}).update(installed=True, enabled=True, status='ready',
                        version=peer['version'], service=peer['service'], last_probe=result)
            self.save(state)
            transaction['status'] = 'committed'; atomic_json(journal, transaction)
        except Exception:
            transaction['status'] = 'recovery_required'; atomic_json(journal, transaction)
            if stopped:
                try:
                    self.runtime.stop(model)
                    self.restore_files(transaction)
                    state = previous
                    for peer in [*peers, model]:
                        state['models'].setdefault(peer['id'], {}).update(enabled=False, status='rolled_back')
                    transaction['status'] = 'rolled_back'; atomic_json(journal, transaction)
                except Exception:
                    state['models'][model['id']]['status'] = 'recovery_required'
            else:
                state['models'][model['id']]['status'] = 'recovery_required'
            self.save(state)
            raise

    def restore_files(self, transaction):
        model = self.catalog.models[transaction['model']]
        allowed = {f['target'] for f in model['files']}
        backup_root = self.store.root / 'backups' / transaction['id']
        for item in transaction['files']:
            if item['target'] not in allowed:
                raise ValueError('备份清单路径不在该模型范围内')
            target = under(self.home, item['target'])
            if item['existed']:
                backup = under(backup_root, item['target'])
                if not backup.is_file():
                    raise ValueError('缺少旧文件备份，需人工核查')
                staging = target.with_name(target.name + '.restore-' + transaction['id'])
                self.copy(backup, staging); staging.replace(target)
            else:
                target.unlink(missing_ok=True)

    def manage(self, ids, action, progress):
        models = self.catalog.select(ids)
        with file_lock(self.deploy / 'state/modelctl.lock'):
            state = self.state()
            for model in models:
                record = state['models'].get(model['id'], {})
                if action == 'rollback':
                    journal = self.store.root / 'transactions' / (model['id'] + '.json')
                    transaction = json.loads(journal.read_text())
                    if transaction.get('model') != model['id'] or not re_uuid(transaction.get('id', '')):
                        raise ValueError('安装事务无效')
                    peers = self.peers(state, model)
                    for peer in [*peers, model]:
                        state['models'].setdefault(peer['id'], {}).update(enabled=False, status='recovery_required')
                    self.save(state)
                    self.runtime.stop(model)
                    self.restore_files(transaction)
                    prior = transaction['previous_state']['models'].get(model['id'], {})
                    state['models'][model['id']] = {**prior, 'enabled': False, 'status': 'rolled_back'}
                    transaction['status'] = 'rolled_back'; atomic_json(journal, transaction)
                    # Recovery keeps a shared service stopped until explicit activation.
                    for peer in peers:
                        if peer['id'] != model['id']:
                            state['models'][peer['id']].update(enabled=False, status='installed')
                elif action == 'disable':
                    record.update(enabled=False, status=record.get('status') if record.get('status') in {'installing', 'recovery_required'} else ('installed' if record.get('installed') else 'not_installed'))
                    state['models'][model['id']] = record
                    self.save(state)
                    if not self.peers(state, model):
                        self.runtime.stop(model)
                elif action == 'activate':
                    if not record.get('installed') or record.get('status') in {'installing', 'recovery_required'}:
                        raise ValueError('请先完成模型安装或恢复旧文件')
                    errors, _ = self.runtime.preflight([model], True)
                    if errors:
                        raise ValueError('；'.join(errors))
                    for item in model['files']:
                        progress({'stage': '启用前校验 SHA-256', 'file': item['target']})
                        self.store.verify(under(self.home, item['target']), item)
                    peers = self.peers(state, model)
                    for peer in [*peers, model]:
                        state['models'].setdefault(peer['id'], {}).update(enabled=False)
                    self.save(state)
                    try:
                        self.runtime.stop(model)
                        self.runtime.start(model, progress)
                        for peer in {m['id']: m for m in [*peers, model]}.values():
                            result = self.runtime.probe(peer)
                            state['models'][peer['id']].update(enabled=True, status='ready', last_probe=result)
                    except Exception:
                        for peer in [*peers, model]:
                            state['models'][peer['id']].update(enabled=False, status='installed')
                        self.save(state)
                        self.runtime.stop(model)
                        raise
                else:
                    raise ValueError('未知的模型操作')
                self.save(state)
        return {'models': ids, 'action': action, 'message': '已恢复旧文件，模型保持停用，请核查后重新启用' if action == 'rollback' else '操作完成'}


def re_uuid(value):
    import re
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value)
