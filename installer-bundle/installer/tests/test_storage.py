"""Bulk-volume selection, private pinned workspaces, and per-filesystem budgets."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import storage
from common import atomic_json,read_json


class StorageTests(unittest.TestCase):
    def test_largest_free_filesystem_selected_with_stable_bind_mount_alias(self):
        paths=[Path('/fixture/system'),Path('/fixture/opt'),Path('/fixture/home')]
        devices={paths[0]:1,paths[1]:2,paths[2]:2}
        free={paths[0]:36,paths[1]:593,paths[2]:594}
        with mock.patch('storage.trusted_directory'),mock.patch('storage.existing',side_effect=lambda p:p),mock.patch('storage.Path.stat',autospec=True,side_effect=lambda p:SimpleNamespace(st_dev=devices[p])),mock.patch('storage.shutil.disk_usage',side_effect=lambda p:SimpleNamespace(free=free[p])):
            self.assertEqual(storage.default_data_root(paths),paths[1])

    def test_workspace_persisted_across_resume_and_cache_shared_between_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);release={'release_id':'fixture','installer_version':'1.2.0'}
            initial=storage.Storage(root/'state',release,root/'bulk');initial.ensure()
            resumed=storage.Storage(root/'state',release,root/'different');resumed.ensure()
            other=storage.Storage(root/'other-state',release,root/'bulk');other.ensure()
            self.assertEqual(initial.work,resumed.work);self.assertEqual(initial.cache,other.cache)
            self.assertNotEqual(initial.work,other.work)
            self.assertEqual(initial.work.stat().st_mode&0o777,0o700)
            self.assertEqual(initial.cache.stat().st_mode&0o777,0o755)
            self.assertEqual(initial.record.stat().st_mode&0o777,0o600)
            self.assertEqual(read_json(initial.record)['data_root'],str(root/'bulk'))

    def test_symlink_or_other_user_writable_work_root_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'target';target.mkdir();link=root/'link';link.symlink_to(target,target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError,'链接'):storage.trusted_directory(link,os.geteuid())
            target.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError,'其他用户'):storage.trusted_directory(target,os.geteuid())

    def test_symlinked_storage_record_never_redirects_private_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'state/private').mkdir(parents=True)
            atomic_json(root/'other.json',{'schema':1,'data_root':str(root/'bulk')})
            (root/'state/private/storage.json').symlink_to(root/'other.json')
            before=(root/'other.json').read_bytes()
            with self.assertRaisesRegex(RuntimeError,'链接'):storage.Storage(root/'state',{'release_id':'fixture'})
            self.assertEqual((root/'other.json').read_bytes(),before)

    def test_budgets_charge_cache_and_work_to_data_volume_and_metadata_to_system(self):
        class Volume:
            def __init__(self,name):self.name=name
            def __str__(self):return self.name
            def stat(self):return SimpleNamespace(st_dev=1 if self.name=='system' else 2)
        with mock.patch('storage.existing',side_effect=lambda p:Volume(p)),mock.patch('storage.shutil.disk_usage',side_effect=lambda p:SimpleNamespace(free=40 if p.name=='system' else 600)):
            rows=storage.budgets([('system',34,'Docker'),('system',1,'metadata'),('bulk',65,'cache'),('bulk',8,'runtime'),('bulk',58,'models')])
        self.assertEqual([r['need_bytes'] for r in rows],[35,131]);storage.require_space(rows)
        rows[0]['free_bytes']=30
        with self.assertRaisesRegex(RuntimeError,'可用.*Docker'):storage.require_space(rows)


if __name__=='__main__':unittest.main(verbosity=2)
