import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe_source
from probe import Probe
from workflow import workflow_assets


def package(binary=None, extra=(), omit=()):
    if binary is None:
        binary=bytearray(64);binary[:7]=b'\x7fELF\x02\x01\x01';struct.pack_into('<H',binary,18,183)
        binary=bytes(binary)
    content={name:(binary if name=='build/data-probe' else b'fixture service or certificate') for name in probe_source.REQUIRED if name not in omit}
    content['config/probe.yaml']=b'bindActivateToken: fixture-upstream-secret'
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as tar:
        for name,data in content.items():
            item=tarfile.TarInfo('data-probe-offline/'+name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
        for item,data in extra:tar.addfile(item,io.BytesIO(data) if data is not None else None)
    return out.getvalue()


class ProbeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.body=package();self.status=200;self.length=None;self.requests=[]
        test=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(dict(self.headers));self.send_response(test.status)
                self.send_header('Content-Length',str(test.length if test.length is not None else len(test.body)))
                self.send_header('Content-Disposition','attachment; filename=data-probe-1.1.2-aarch64-offline.tar.gz')
                self.end_headers();self.wfile.write(test.body)
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        patcher=mock.patch.object(probe_source,'PROBE_URL',f'http://127.0.0.1:{self.server.server_port}/download')
        patcher.start();self.addCleanup(patcher.stop)
        self.flow=SimpleNamespace(directory=self.root/'work',owner=SimpleNamespace(state={},event=mock.Mock(),update=mock.Mock(),pause_point=mock.Mock()))

    def test_current_arm64_program_is_checked_and_upstream_binding_is_not_imported(self):
        result=probe_source.fetch_probe(self.flow)
        self.assertEqual(result['version'],'1.1.2')
        self.assertEqual(result['archive_sha256'],hashlib.sha256(self.body).hexdigest())
        self.assertEqual(result['binary_sha256'],hashlib.sha256((result['source']/'build/data-probe').read_bytes()).hexdigest())
        self.assertFalse((result['source']/'config/probe.yaml').exists())
        self.assertNotIn('fixture-upstream-secret',json.dumps(self.flow.owner.state))
        self.assertEqual(self.requests[0]['Cache-Control'],'no-cache')
        self.assertEqual(result['source'].parent.stat().st_mode&0o777,0o700)

    def test_each_attempt_refetches_even_when_version_name_is_unchanged(self):
        first=probe_source.fetch_probe(self.flow)
        binary=(first['source']/'build/data-probe').read_bytes()+b'new-build'
        self.body=package(binary)
        second=probe_source.fetch_probe(self.flow)
        self.assertEqual(len(self.requests),2);self.assertEqual(first['version'],second['version'])
        self.assertNotEqual(first['binary_sha256'],second['binary_sha256'])

    def test_existing_matching_probe_is_compared_only_after_successful_download(self):
        binary=self.root/'data-probe'
        with tarfile.open(fileobj=io.BytesIO(self.body)) as tar:binary.write_bytes(tar.extractfile('data-probe-offline/build/data-probe').read())
        self.flow.settings={'probe':{'mode':'auto','reuse_existing':True}}
        adapter=Probe(self.flow,config_root=self.root/'config',binary=binary,unit_root=self.root/'units')
        with mock.patch.object(adapter,'activate',side_effect=AssertionError('matching probe must remain untouched')):
            adapter.install()
        self.assertEqual(len(self.requests),1);self.assertTrue(self.flow.owner.state['probe_skipped'])

    def test_failed_download_does_not_replace_old_probe_or_fall_back_to_bundle(self):
        binary=self.root/'data-probe';binary.write_bytes(b'old-build');self.status=503
        self.flow.settings={'probe':{'mode':'auto','reuse_existing':True}}
        self.flow.run=mock.Mock();self.flow.asset=mock.Mock()
        with self.assertRaisesRegex(RuntimeError,'无法下载'):
            Probe(self.flow,config_root=self.root/'config',binary=binary,unit_root=self.root/'units').install()
        self.assertEqual(binary.read_bytes(),b'old-build');self.flow.run.assert_not_called();self.flow.asset.assert_not_called()
        self.assertFalse(list(self.flow.directory.glob('probe-online-*')))

    def test_short_response_and_oversized_content_length_are_rejected(self):
        for length in [len(self.body)+1,probe_source.MAX_DOWNLOAD+1]:
            with self.subTest(length=length):
                self.length=length
                with self.assertRaises(RuntimeError):probe_source.fetch_probe(self.flow)
                self.assertFalse(list(self.flow.directory.glob('probe-online-*')))

    def test_missing_required_file_and_wrong_architecture_are_rejected(self):
        self.body=package(omit=['deploy/data-probe.service'])
        with self.assertRaisesRegex(RuntimeError,'缺少必要'):probe_source.fetch_probe(self.flow)
        self.body=package(b'not-an-arm64-elf')
        with self.assertRaisesRegex(RuntimeError,'ARM64 ELF'):probe_source.fetch_probe(self.flow)

    def test_parent_escape_symlink_and_duplicate_members_are_rejected(self):
        escape=tarfile.TarInfo('data-probe-offline/../../outside');escape.size=1
        link=tarfile.TarInfo('data-probe-offline/other');link.type=tarfile.SYMTYPE;link.linkname='/etc'
        duplicate=tarfile.TarInfo('data-probe-offline/build/data-probe');duplicate.size=1
        for item,data in [(escape,b'x'),(link,None),(duplicate,b'x')]:
            with self.subTest(member=item.name):
                self.body=package(extra=[(item,data)])
                with self.assertRaises((RuntimeError,ValueError)):probe_source.fetch_probe(self.flow)
        self.assertFalse((self.root/'outside').exists())

    def test_pause_is_propagated_and_partial_download_is_removed(self):
        self.flow.owner.pause_point.side_effect=RuntimeError('fixture paused')
        with self.assertRaisesRegex(RuntimeError,'fixture paused'):probe_source.fetch_probe(self.flow)
        self.assertFalse(list(self.flow.directory.glob('probe-online-*')))

    def test_legacy_signed_probe_snapshot_is_not_a_distribution_asset(self):
        lock={'probe':{'archive':{'path':'workflow/probe/old.tar.gz'}},'images':[],
              'host_packages':[{'path':'workflow/host-debs/required.deb'}]}
        self.assertEqual(workflow_assets(lock),lock['host_packages'])


if __name__=='__main__':unittest.main(verbosity=2)
