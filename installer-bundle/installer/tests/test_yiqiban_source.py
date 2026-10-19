"""Exercise the real ZIP/template importer and the separate pinned OCI download."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from artifacts import SourceError, source_config, oci_source
from yiqiban import YiQiBan
from yiqiban_payload import load, validate_files
from yiqiban_source import acquisition_config, ensure, reservations, resource_root, settings, unpack

BUNDLE = Path(__file__).resolve().parents[2]


def descriptor(data, path=None):
    row = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    if path is not None:row['path'] = path
    return row


class YiQiBanSourceTests(unittest.TestCase):
    def fixture(self, root, *, entries=None, tar_entries=None, mutation=None):
        bundle = root / 'bundle';(bundle / 'yiqiban').mkdir(parents=True);(bundle / 'catalog').mkdir()
        for name in ['yiqiban/manifest.json', 'yiqiban/manifest.sig', 'catalog/release-public.pem']:
            (bundle / name).write_bytes(('fixture ' + name).encode())
        image = b'fixture authenticated docker archive';template = b'fixture database schema'
        rows = [descriptor(image, 'app.tar'), descriptor(template, 'sql/schema.sql')]
        manifest = {'files': rows, 'images': [{'archive': 'app.tar'}]}
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            for name, data, kind in tar_entries or [('files/sql/schema.sql', template, tarfile.REGTYPE)]:
                item = tarfile.TarInfo(name);item.type = kind;item.size = len(data)
                if kind == tarfile.SYMTYPE:item.linkname = '/outside'
                tar.addfile(item, io.BytesIO(data))
        templates = raw.getvalue();archive = root / 'application.zip';prefix = 'release/upload/'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for name, local in [('manifest.json', 'yiqiban/manifest.json'), ('manifest.sig', 'yiqiban/manifest.sig'),
                                ('release-public.pem', 'catalog/release-public.pem')]:
                zipped.writestr(prefix + name, (bundle / local).read_bytes())
            zipped.writestr(prefix + 'templates.tar.gz', templates)
            zipped.writestr(prefix + 'files/app.tar', image)
            # Upload tooling must never be installed or executed by the importer.
            zipped.writestr('release/tools/do-not-run.exe', b'ignored upload-only tool')
            for name, value in entries or []:zipped.writestr(name, value)
        spec = {'archive': descriptor(archive.read_bytes()), 'templates': descriptor(templates), 'zip_prefix': prefix}
        if mutation:mutation(spec, manifest)
        destination = root / 'stage';destination.mkdir(mode=0o700)
        return bundle, archive, spec, manifest, destination

    def test_real_zip_and_tar_produce_only_signed_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle, archive, spec, manifest, output = self.fixture(Path(temp))
            unpack(bundle, archive, spec, manifest, output)
            validate_files(manifest, output / 'files')
            self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()},
                             {'files/app.tar', 'files/sql/schema.sql'})

    def test_wrong_zip_digest_fails_before_any_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.fixture(Path(temp));args[2]['archive']['sha256'] = '0' * 64
            with self.assertRaisesRegex(RuntimeError, 'ZIP.*摘要'):unpack(*args)
            self.assertFalse(any(args[-1].iterdir()))

    def test_zip_uses_installer_trust_root_and_exact_signed_manifest(self):
        for path in ['yiqiban/manifest.json', 'yiqiban/manifest.sig', 'catalog/release-public.pem']:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp));(args[0] / path).write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, '签名或公钥'):unpack(*args)

    def test_zip_rejects_traversal_duplicates_and_symlinks_even_in_ignored_tooling(self):
        link = zipfile.ZipInfo('release/tools/link');link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entry in [('../outside', b'x'), ('release/tools/do-not-run.exe', b'duplicate'), (link, b'/outside')]:
            with self.subTest(entry=str(entry[0])), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp), entries=[entry])
                with self.assertRaises((RuntimeError, ValueError)):unpack(*args)

    def test_template_rejects_links_unsigned_files_and_duplicate_paths(self):
        for entries in [[('files/sql/schema.sql', b'', tarfile.SYMTYPE)],
                        [('files/other', b'unsigned', tarfile.REGTYPE)],
                        [('files/sql/schema.sql', b'fixture database schema', tarfile.REGTYPE)] * 2]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp), tar_entries=entries)
                with self.assertRaisesRegex(RuntimeError, '模板'):unpack(*args)

    def test_missing_template_and_signed_digest_mismatch_cannot_pass(self):
        for entries in [[('files/', b'', tarfile.DIRTYPE)], [('files/sql/schema.sql', b'wrong contents', tarfile.REGTYPE)]]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                args = self.fixture(Path(temp), tar_entries=entries)
                with self.assertRaises(RuntimeError):unpack(*args)

    def test_slim_bundle_can_verify_metadata_without_installed_images(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp);(bundle / 'yiqiban').mkdir();(bundle / 'catalog').mkdir()
            for name in ['release.json', 'yiqiban/manifest.json', 'yiqiban/manifest.sig', 'yiqiban/source.json', 'catalog/release-public.pem']:
                shutil.copyfile(BUNDLE / name, bundle / name)
            manifest = load(bundle)
            self.assertEqual(len(manifest['images']), 10)
            self.assertEqual(settings(bundle)['release_id'], manifest['release_id'])
            with self.assertRaisesRegex(RuntimeError, '缺失'):load(bundle, verify_files=True)

    def test_source_rejects_wrong_version_or_unsigned_digest_routing(self):
        original = json.loads((BUNDLE / 'yiqiban/source.json').read_text())
        for key, value in [('release_id', 'other'), ('manifest_sha256', '0' * 64)]:
            spec = dict(original, **{key: value})
            with patch('yiqiban_source.read_json', return_value=spec), self.assertRaisesRegex(RuntimeError, '版本'):
                settings(BUNDLE)
        config = acquisition_config(original, ['/local/models'])
        self.assertIsNotNone(oci_source(source_config(BUNDLE, config), original['archive']))
        self.assertEqual(config['mode'], 'prefer-local')

    def test_acquisition_failure_precedes_database_images_and_service_mutations(self):
        app = YiQiBan.__new__(YiQiBan);app.owner = object()
        app.initialize = lambda: self.fail('database initialization must not run')
        with patch('yiqiban_source.ensure', side_effect=SourceError('download interrupted')):
            with self.assertRaisesRegex(SourceError, 'interrupted'):app.deploy()

    def test_bulk_partition_budget_includes_archive_and_expanded_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);storage = SimpleNamespace(work=root/'bulk/work',cache=root/'bulk/cache')
            spec = {'archive': dict(descriptor(b'zip'),path='application.zip'), 'templates':descriptor(b'templates')}
            rows = reservations(storage,spec,{'files':[descriptor(b'image and config')]})
            self.assertEqual(rows[0][0],storage.cache);self.assertEqual(rows[0][1],3)
            self.assertEqual(rows[1][0],storage.work);self.assertEqual(rows[1][1],len(b'image and config')+len(b'templates'))

    def test_verified_cache_is_reused_without_network_and_corruption_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);storage = SimpleNamespace(work=root / 'work', cache=root / 'cache')
            storage.work.mkdir(mode=0o700);storage.cache.mkdir()
            spec = {'manifest_sha256': 'a' * 64};target = resource_root(storage, spec)
            (target / 'files').mkdir(parents=True);data = b'cached signed image'
            (target / 'files/app.tar').write_bytes(data)
            manifest = {'files': [descriptor(data, 'app.tar')]}
            owner = SimpleNamespace(bundle=BUNDLE, root=root/'state', storage=storage, pause_point=lambda: None, event=lambda x: None)
            with patch('yiqiban_source.settings', return_value=spec), patch('yiqiban_source.load', return_value=manifest), patch('yiqiban_source.BlobStore') as store:
                self.assertEqual(ensure(owner), target / 'files');store.assert_not_called()
                (target / 'files/app.tar').write_bytes(b'corrupt')
                with patch('yiqiban_source.read_json', side_effect=RuntimeError('stop before network')):
                    with self.assertRaisesRegex(RuntimeError, 'before network'):ensure(owner)
                self.assertFalse(target.exists())
                self.assertEqual(len(list(storage.work.glob('*-invalid-*'))), 1)

    def test_pause_during_cache_validation_does_not_quarantine_valid_cache(self):
        class Paused(Exception):pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);storage = SimpleNamespace(work=root / 'work', cache=root / 'cache')
            storage.work.mkdir(mode=0o700);storage.cache.mkdir()
            spec = {'manifest_sha256': 'a' * 64};target = resource_root(storage, spec);(target / 'files').mkdir(parents=True)
            (target / 'files/app.tar').write_bytes(b'x')
            owner = SimpleNamespace(bundle=BUNDLE, storage=storage, pause_point=lambda: (_ for _ in ()).throw(Paused()), event=lambda x: None)
            with patch('yiqiban_source.settings', return_value=spec), patch('yiqiban_source.load', return_value={'files': [descriptor(b'x', 'app.tar')]}):
                with self.assertRaises(Paused):ensure(owner)
            self.assertTrue(target.exists())


if __name__ == '__main__':unittest.main()
