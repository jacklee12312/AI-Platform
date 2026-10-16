"""Apply the same takeover policy to published gateway/ModelDeck ports."""
import json
import os
from pathlib import Path
import re

from common import CORE_DIRS
from port_takeover import Takeover

UNITS = ['houmo-ai.service', 'ai-model-gateway.service', 'gateway-admin.service',
         'modeldeck-ops.service', 'modeldeck-startup-verify.service']


def names(installer):
    result = set()
    for p in (installer.resources / 'payload/home/cix').rglob('*'):
        if p.name in ['compose.yaml', 'docker-compose.yml']:
            result.update(re.findall(r'^\s*container_name:\s*([\w.-]+)\s*$', p.read_text(), re.M))
    return result


def prepare_files(installer):
    transaction = Takeover(installer.workflow, 'platform-files')
    if transaction.record and transaction.record['status'] == 'committed': return
    if transaction.record and transaction.record['status'] != 'rolled-back': transaction.rollback()
    transaction.begin()
    try:
        transaction.claim([17700, 17780], names=names(installer), units=UNITS)
        for name in CORE_DIRS: transaction.archive(installer.home / name)
        volume = transaction.run('docker', 'volume', 'inspect', 'gateway-admin_admin-data', check=False)
        if volume.returncode == 0:
            info = json.loads(volume.stdout)[0]
            if (info.get('Labels') or {}).get('com.docker.compose.project') != 'gateway-admin':
                raise RuntimeError('管理中心数据卷归属无法确认')
            for container in transaction.containers():
                if container['State']['Running'] and any(m.get('Name') == info['Name'] for m in container['Mounts']):
                    transaction.stop_container(container)
            data = Path(info['Mountpoint'])
            if data.name != '_data' or data.parent.name != info['Name']: raise RuntimeError('数据卷位置无效')
            transaction.archive(data)
            data.mkdir(parents=True, exist_ok=True)
        for unit in UNITS: transaction.release_unit(unit)
        for pin in installer.manifest['images']:
            result = transaction.run('docker', 'image', 'inspect', pin['tag'], check=False)
            if result.returncode == 0:
                info = json.loads(result.stdout)[0]
                if info['Id'] not in {pin['id'], pin['config_id']}:
                    transaction.record['images'][pin['tag']] = info['Id'];transaction.save()
                    transaction.run('docker', 'tag', info['Id'], 'ai-platform-backup/image:' + info['Id'].split(':')[1])
                    transaction.run('docker', 'image', 'rm', pin['tag'])
        transaction.run('systemctl', 'daemon-reload')
        link = Path('/usr/local/bin/modelctl')
        if link.is_symlink() and link.resolve(strict=False) == installer.home / 'houmo-docker-deployment/modelctl':
            link.unlink()
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise


def claim_ports(installer):
    transaction = Takeover(installer.workflow, 'platform-ports')
    if transaction.record and transaction.record['status'] not in ('committed', 'rolled-back'):
        transaction.rollback()
    transaction.begin()
    expected = names(installer)
    owned = []
    ports = {17700, 17780}
    pins = {r[k] for r in installer.manifest['images'] for k in ('id', 'config_id') if r.get(k)}
    for info in transaction.containers():
        labels = info['Config'].get('Labels') or {}
        directory = labels.get('com.docker.compose.project.working_dir', '')
        if (installer.state.get('start_attempted') and info['Name'].strip('/') in expected and
                info['Image'] in pins and Path(directory).parent == installer.home):
            owned.append(info['Id'])
            if info['State']['Running']:
                ports.difference_update(int(x['HostPort']) for values in (info['HostConfig'].get('PortBindings') or {}).values()
                                        for x in values or [] if x.get('HostPort', '').isdigit())
    transaction.claim(ports, names=expected, owned=owned)
    return transaction


def rollback_start(installer, transaction):
    expected = names(installer)
    pins = {r[k] for r in installer.manifest['images'] for k in ('id','config_id') if r.get(k)}
    for name in UNITS:
        transaction.run('systemctl', 'stop', name, check=False)
    for info in transaction.containers():
        labels = info['Config'].get('Labels') or {}
        directory = labels.get('com.docker.compose.project.working_dir','')
        if info['Name'].strip('/') in expected and info['Image'] in pins and Path(directory).parent == installer.home:
            transaction.created(info)
    transaction.rollback()
