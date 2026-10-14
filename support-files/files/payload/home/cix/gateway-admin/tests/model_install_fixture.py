"""UI-only simulated device. Never used by production; downloads nothing."""
import time
import uuid
import copy
from agent.model_store import Catalog


class Fixture:
    def __init__(self, owner, jobs):
        self.owner, self.jobs = owner, jobs
        self.rows = [{**{k: m[k] for k in ('id', 'capability', 'device', 'version', 'modes', 'image', 'download_bytes')},
                      'cached_bytes': 0, 'downloaded': False, 'installed': False, 'enabled': False,
                      'status': 'not_installed', 'image_ready': m['device'] == 'CPU',
                      'mode_compatible': not m['modes'] or 'multi-model' in m['modes'], 'rollback_available': False}
                     for m in Catalog().models.values()]
        self.initial_rows = copy.deepcopy(self.rows)

    def reset(self):
        self.rows = copy.deepcopy(self.initial_rows)
        return {'fixture': True}

    async def handle(self, method, path, body):
        if method == 'GET' and path == '/v1/model-install':
            return {'models': self.rows, 'mode': 'multi-model', 'managed': True, 'storage_free_bytes': 150 * 1024 ** 3, 'uploads': []}
        ids = body.get('ids', [])
        rows = [m for m in self.rows if m['id'] in ids]
        if len(rows) != len(ids) or not rows:
            raise self.owner.AgentRequestError(422, 'invalid fixture selection')
        if path.endswith('/plan'):
            errors = [f"缺少运行镜像：{m['image']}" for m in rows if body['activate'] and not m['image_ready']]
            return {'valid': not errors, 'errors': errors, 'notices': [],
                    'download_bytes': sum(m['download_bytes'] - m['cached_bytes'] for m in rows),
                    'required_free_bytes': sum(m['download_bytes'] for m in rows), 'steps': ['校验文件', '安装权重', '保存状态']}
        action = path.rsplit('/', 1)[-1]
        if action == 'download':
            for row in rows: row.update(downloaded=True, cached_bytes=row['download_bytes'])
        elif action == 'install':
            for row in rows: row.update(installed=True, downloaded=True, cached_bytes=row['download_bytes'], status='installed', rollback_available=True)
        elif action == 'activate':
            for row in rows: row.update(enabled=True, status='ready')
        elif action == 'disable':
            for row in rows: row.update(enabled=False, status='installed')
        else: raise self.owner.AgentRequestError(400, 'unsupported fixture operation')
        job = {'id': uuid.uuid4().hex, 'kind': 'model-' + action, 'target': ','.join(ids), 'status': 'queued', 'created_at': self.owner._now()}
        self.jobs[job['id']] = {**job, 'clock': time.monotonic()}
        return job
