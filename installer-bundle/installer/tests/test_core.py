import io,json,os,sys,tarfile,tempfile,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common
import engine

class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.src=self.root/'source';self.dst=self.root/'dest';self.src.mkdir();self.dst.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def source(self,data=b'hello'):
        p=self.src/'model.bin';p.write_bytes(data);return p
    def test_bad_paths(self):
        for value in ['/etc/passwd','../escape','a/../../escape','a\\b','C:/bad','', 'a\x00b']:
            with self.subTest(value=value),self.assertRaises(ValueError):common.safe_relative(value)
    def test_valid_paths(self):
        for p in ['a/b','./models/one.gguf','models/中文.onnx']:self.assertFalse(common.safe_relative(p).is_absolute())
    def test_atomic_copy_and_hash(self):
        s=self.source();t=self.dst/'nested/model.bin'
        self.assertEqual(common.copy_atomic(s,t,self.dst),'created');self.assertEqual(t.read_bytes(),b'hello')
        self.assertEqual(common.copy_atomic(s,t,self.dst),'reused')
    def test_new_dirs_readable_under_private_umask(self):
        old=os.umask(0o077)
        try:common.copy_atomic(self.source(),self.dst/'nested/model',self.dst)
        finally:os.umask(old)
        self.assertEqual((self.dst/'nested').stat().st_mode & 0o777,0o755)
    def test_conflict_never_overwrites(self):
        s=self.source();t=self.dst/'model.bin';t.write_bytes(b'other')
        with self.assertRaises(RuntimeError):common.copy_atomic(s,t,self.dst)
        self.assertEqual(t.read_bytes(),b'other')
    def test_symlink_parent_escape(self):
        s=self.source();(self.dst/'link').symlink_to(self.src,target_is_directory=True)
        with self.assertRaises(RuntimeError):common.copy_atomic(s,self.dst/'link/other',self.dst)
        self.assertFalse((self.src/'other').exists())
    def test_final_symlink_not_followed(self):
        s=self.source();t=self.dst/'model.bin';t.symlink_to(s)
        with self.assertRaises(RuntimeError):common.copy_atomic(s,t,self.dst)
    def test_root_escape(self):
        with self.assertRaises(RuntimeError):common.no_symlink_parents(self.root/'outside',self.dst)
    def test_extra_models_preserved(self):
        extra=self.dst/'extra.gguf';extra.write_bytes(b'unrelated')
        common.copy_atomic(self.source(),self.dst/'package.bin',self.dst)
        self.assertEqual(extra.read_bytes(),b'unrelated')
    def test_atomic_json(self):
        p=self.dst/'journal.json';common.atomic_json(p,{'stage':'payload'});self.assertEqual(common.read_json(p)['stage'],'payload')
    def test_reused_file_permission_unchanged(self):
        s=self.source();t=self.dst/'m';t.write_bytes(s.read_bytes());t.chmod(0o644)
        before=t.stat().st_mode;common.copy_atomic(s,t,self.dst,mode=0o755)
        self.assertEqual(t.stat().st_mode,before)
    def test_partial_copy_not_published(self):
        s=self.source(b'x'*100);t=self.dst/'model'
        def fail(*args):raise OSError('simulated power interruption')
        with self.assertRaises(OSError):common.copy_atomic(s,t,self.dst,callback=fail)
        self.assertFalse(t.exists());self.assertEqual(list(self.dst.iterdir()),[])
    def make_tar(self,entries):
        p=self.src/'runtime.tar.gz'
        with tarfile.open(p,'w:gz') as tf:
            for name,kind,data in entries:
                info=tarfile.TarInfo(name)
                if kind=='file':info.size=len(data);tf.addfile(info,io.BytesIO(data))
                else:info.type=tarfile.SYMTYPE;info.linkname=data;tf.addfile(info)
        return p
    def test_archive_traversal_rejected(self):
        p=self.make_tar([('../evil','file',b'x')])
        with self.assertRaises(ValueError):engine.safe_extract(p,self.dst)
    def test_archive_symlink_escape_rejected(self):
        p=self.make_tar([('a/link','link','../../outside')])
        with self.assertRaises(RuntimeError):engine.safe_extract(p,self.dst)
    def test_archive_symlink_parent_rejected(self):
        p=self.make_tar([('link','link','/home/cix/models'),('link/evil','file',b'x')])
        with self.assertRaises((FileExistsError,RuntimeError)):engine.safe_extract(p,self.dst)
    def test_valid_runtime_files(self):
        p=self.make_tar([('a/file','file',b'x'),('a/link','link','file')]);engine.safe_extract(p,self.dst)
        self.assertEqual((self.dst/'a/file').read_bytes(),b'x');self.assertTrue((self.dst/'a/link').is_symlink())
    def test_password_rules(self):
        common.check_password('a-valid-password')
        for p in ['short',None,'x'*129,'abcdefghijkl\n']:
            with self.assertRaises(ValueError):common.check_password(p)
    def test_full_scope_stages(self):
        self.assertEqual(len(common.STEPS),16)
        self.assertEqual([x[0] for x in common.STEPS[:3]],['probe','components','acquire'])
        self.assertEqual([x[0] for x in common.STEPS[-4:]],['acceptance','yiqiban','yiqiban-check','integration'])
        self.assertEqual(common.STEPS[-1][0],'integration')
    def test_pause_boundary(self):
        obj=object.__new__(engine.Installer);obj.root=self.dst;(self.dst/'private').mkdir();(self.dst/'private/pause.request').touch()
        with self.assertRaises(engine.Paused):obj.pause_point()
    def test_acceptance_selected_models(self):
        obj=object.__new__(engine.Installer);obj.resources=self.src;obj.home=self.dst;obj.state={};obj.command=mock.Mock();obj.event=mock.Mock();obj.update=mock.Mock();obj.selected=['copaw-flash-9b'];obj.device_count=1
        p=self.dst/'houmo-docker-deployment/state/offline-acceptance.json';p.parent.mkdir(parents=True);p.write_text('{"passed":true}')
        with mock.patch.object(obj,'validate_selected_acceptance') as validate:obj.acceptance()
        self.assertIn('--selected-json',obj.command.call_args.args)
        self.assertEqual(len(obj.command.call_args_list),1)
        validate.assert_called_once_with({'passed':True},obj.state['acceptance_run_id'])
    def test_acceptance_cannot_false_pass(self):
        obj=object.__new__(engine.Installer);obj.resources=self.src;obj.home=self.dst;obj.state={};obj.command=mock.Mock();obj.event=mock.Mock();obj.update=mock.Mock();obj.selected=['copaw-flash-9b'];obj.device_count=1
        p=self.dst/'houmo-docker-deployment/state/offline-acceptance.json';p.parent.mkdir(parents=True);p.write_text('{"passed":false}')
        with self.assertRaises(RuntimeError):obj.acceptance()
    def credential_object(self):
        obj=object.__new__(engine.Installer);obj.home=self.dst;obj.root=self.src;obj.event=mock.Mock();obj.selected=['copaw-flash-9b']
        for name in ['houmo-docker-deployment','gateway-admin','ai-model-gateway','ballot-reader']:(self.dst/name).mkdir(exist_ok=True)
        common.atomic_json(self.src/'private/request.json',{'admin_password':'valid-test-password'})
        return obj
    def test_credentials_retry_preserves_keys_and_hash(self):
        obj=self.credential_object();user=mock.Mock(pw_uid=os.getuid(),pw_gid=os.getgid())
        with mock.patch('engine.pwd.getpwnam',return_value=user),mock.patch('engine.os.chown'):
            obj.credentials();first=common.read_json(self.src/'private/request.json');obj.credentials()
        self.assertNotIn('admin_password',first);self.assertEqual(first,common.read_json(self.src/'private/request.json'))
        self.assertEqual((self.dst/'gateway-admin/.env').stat().st_mode & 0o777,0o600)
        self.assertFalse((self.dst/'gateway-admin/INITIAL_ADMIN_PASSWORD').exists())
    def test_credentials_resume_after_partial_publication(self):
        obj=self.credential_object();user=mock.Mock(pw_uid=os.getuid(),pw_gid=os.getgid());real_link=os.link
        def partial(source,target):
            if 'gateway-admin' in str(target):raise OSError('simulated power loss')
            return real_link(source,target)
        with mock.patch('engine.pwd.getpwnam',return_value=user),mock.patch('engine.os.chown'):
            with mock.patch('engine.os.link',side_effect=partial),self.assertRaises(OSError):obj.credentials()
            saved=common.read_json(self.src/'private/request.json');obj.credentials()
        self.assertEqual(saved,common.read_json(self.src/'private/request.json'))
    def test_credentials_do_not_overwrite_foreign_env(self):
        obj=self.credential_object();p=self.dst/'gateway-admin/.env';p.write_text('unrelated-secret');p.chmod(0o600)
        with mock.patch('engine.pwd.getpwnam',return_value=mock.Mock(pw_uid=os.getuid(),pw_gid=os.getgid())),mock.patch('engine.os.chown'),self.assertRaises(RuntimeError):obj.credentials()
        self.assertEqual(p.read_text(),'unrelated-secret')
    def test_resumed_unit_conflict_blocked(self):
        obj=object.__new__(engine.Installer);obj.resources=self.src
        p=self.dst/'houmo-ai.service';p.write_text('foreign service')
        source=self.src/'payload/home/cix/houmo-docker-deployment/houmo-ai.service';source.parent.mkdir(parents=True);source.write_text('ours')
        with self.assertRaises(RuntimeError):obj.guard_units(self.dst)
        self.assertEqual(p.read_text(),'foreign service')


    def test_start_reconciles_model_after_active_oneshot(self):
        obj=object.__new__(engine.Installer);obj.home=self.dst;obj.state={};obj.update=mock.Mock();obj.base=mock.Mock();obj.command=mock.Mock();obj.selected=['copaw-flash-9b']
        user=mock.Mock(pw_uid=os.getuid(),pw_gid=os.getgid())
        with mock.patch('engine.pwd.getpwnam',return_value=user),mock.patch('engine.os.chown'),mock.patch('npu_runtime.configure') as npu:
            obj.start();npu.assert_called_once_with(obj)
        obj.base.compose.assert_called_once();self.assertIn(mock.call('/usr/local/bin/modelctl','boot',timeout=720),obj.command.call_args_list)
        self.assertTrue(obj.state['start_attempted'])
    def test_start_does_not_boot_after_platform_start_failure(self):
        obj=object.__new__(engine.Installer);obj.home=self.dst;obj.state={};obj.update=mock.Mock();obj.base=mock.Mock();obj.command=mock.Mock();obj.selected=['copaw-flash-9b']
        obj.base.compose.side_effect=RuntimeError('platform failed')
        with mock.patch('npu_runtime.configure'),self.assertRaises(RuntimeError):obj.start()
        obj.command.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
