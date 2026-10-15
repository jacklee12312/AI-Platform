"""Apply and rollback upgrades inside a temporary filesystem; systemctl is simulated."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'installer-bundle/installer'))
import artifacts


class UpgradeTests(unittest.TestCase):
    def execute(self, fail=False, corrupt=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);base=Path(temp.name);root=base/'root'
        home=root/'home/cix';bundle=base/'bundle';resources=base/'resources'
        def write(p,data):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        write(home/'gateway-admin/.env',b'fixture-only-password')
        write(home/'houmo-docker-deployment/state/model-installations.json',b'preserved-state')
        write(home/'gateway-admin/frontend/dist/index.html',b'old-ui')
        path='payload/home/cix/gateway-admin/agent/model_store.py'
        target=root/'home/cix/gateway-admin/agent/model_store.py';write(target,b'old-source')
        write(resources/path,b'new-source');unit='systemd/modeldeck-ops.service';write(resources/unit,b'new-unit')
        ui=resources/'runtime/ui-static.tar.gz';ui.parent.mkdir(parents=True,exist_ok=True)
        with tarfile.open(ui,'w:gz') as tar:
            entry=tarfile.TarInfo('index.html');entry.size=6;tar.addfile(entry,io.BytesIO(b'new-ui'))
        rows=[{'path':p,'sha256':hashlib.sha256((resources/p).read_bytes()).hexdigest()} for p in [path,unit,'runtime/ui-static.tar.gz']]
        old={'resources':[{'path':path,'sha256':hashlib.sha256(b'old-source').hexdigest()}]}
        write(bundle/'catalog/upgrade-baseline.json',json.dumps(old).encode())
        if corrupt:write(resources/path,b'corrupted')
        module_spec=importlib.util.spec_from_file_location('upgrade_under_test',ROOT/'installer-bundle/tools/upgrade_modeldeck.py')
        module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
        def paths(value):
            value=str(value)
            if value=='/':return root
            if value in ['/home/cix','/dev/xh2a_ipu1','/etc/systemd/system/modeldeck-ops.service','/var/lib/modeldeck-ops']:
                return root/value.lstrip('/')
            return Path(value)
        calls=[];starts=0
        def command(args,**kwargs):
            nonlocal starts
            calls.append(args)
            if args==['systemctl','start','gateway-admin.service']:
                starts+=1
                if fail and starts==1:raise subprocess.CalledProcessError(1,args)
            return SimpleNamespace(returncode=0)
        with (patch.object(module,'Path',side_effect=paths),patch.object(module.os,'geteuid',return_value=0),
              patch.object(module.os,'chown'),patch.object(module.platform,'machine',return_value='aarch64'),
              patch.object(module.subprocess,'run',side_effect=command),patch.object(artifacts,'load_catalog',return_value={'resources':rows}),
              patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=1000,pw_gid=1000)),
              patch.object(sys,'argv',['upgrade','--bundle',str(bundle),'--resources',str(resources),'--apply']),
              contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO())):
            if fail or corrupt:
                with self.assertRaises(RuntimeError):module.main()
            else:module.main()
        self.assertEqual((home/'gateway-admin/.env').read_bytes(),b'fixture-only-password')
        self.assertEqual((home/'houmo-docker-deployment/state/model-installations.json').read_bytes(),b'preserved-state')
        return home,target,calls

    def test_upgrade_applies_verified_source_and_ui_without_changing_credentials(self):
        home,target,calls=self.execute()
        self.assertEqual(target.read_bytes(),b'new-source');self.assertEqual((home/'gateway-admin/frontend/dist/index.html').read_bytes(),b'new-ui')
        self.assertIn(['systemctl','enable','--now','modeldeck-ops.service'],calls)

    def test_failed_service_start_restores_source_ui_and_original_service(self):
        home,target,calls=self.execute(fail=True)
        self.assertEqual(target.read_bytes(),b'old-source');self.assertEqual((home/'gateway-admin/frontend/dist/index.html').read_bytes(),b'old-ui')
        self.assertEqual(calls.count(['systemctl','start','gateway-admin.service']),2)

    def test_bad_digest_is_rejected_before_stopping_any_service(self):
        home,target,calls=self.execute(corrupt=True)
        self.assertEqual(target.read_bytes(),b'old-source');self.assertEqual(calls,[])
