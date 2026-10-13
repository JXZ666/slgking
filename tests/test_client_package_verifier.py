"""Fail-closed frozen-client verification without executing bundled modules."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import verify_client_package as package


class ClientPackageVerifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.exe = self.root / 'verification.exe'
        self.exe.write_bytes(b'synthetic archive')
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE games(slug TEXT,origin TEXT)')
        self.seed = conn.serialize()
        conn.close()
        self.code = {name: compile('marker = 1', name + '.py', 'exec') for name in package.REQUIRED_MODULES}
        self.code['slg_gui'] = compile('APP_VERSION="0.26.0"\nTEST_APP_VERSION="0.26.0"', 'gui.py', 'exec')
        owner = self
        class Pyz:
            @property
            def toc(self):
                return owner.code
            def extract(self, name):
                return owner.code[name]
        class Archive:
            toc = {'PYZ.pyz': ('z',), 'assets/seed/slgking.db': ('x',),
                   'assets/dlsite/slgking.db': ('x',), 'assets/dlsite/manifest.json': ('x',)}
            def __init__(self, path):
                pass
            def open_embedded_archive(self, name):
                return Pyz()
            def extract(self, name):
                if name.startswith('assets/dlsite/'):
                    return (Path(__file__).resolve().parents[1] / name).read_bytes()
                return owner.seed
        self.archive = patch.object(package, 'CArchiveReader', Archive)
        self.archive.start()
        self.addCleanup(self.archive.stop)

    def test_current_channels_and_source_status_module_are_required(self):
        result = package.verify(self.exe)
        self.assertEqual(result['versions'], {'APP_VERSION': '0.26.0', 'TEST_APP_VERSION': '0.26.0'})
        self.assertTrue(result['required_modules']['slg_source_status'])
        del self.code['slg_source_status']
        with self.assertRaisesRegex(ValueError, 'module is missing'):
            package.verify(self.exe)

    def test_old_test_version_is_rejected_by_default(self):
        self.code['slg_gui'] = compile('APP_VERSION="0.26.0"\nTEST_APP_VERSION="0.25.7"', 'gui.py', 'exec')
        with self.assertRaisesRegex(ValueError, 'versions'):
            package.verify(self.exe)

    def test_embedded_personal_seed_records_are_rejected(self):
        conn = sqlite3.connect(':memory:')
        conn.deserialize(self.seed)
        conn.execute("INSERT INTO games VALUES('user-private','user')")
        conn.commit()
        self.seed = conn.serialize()
        conn.close()
        with self.assertRaisesRegex(ValueError, 'personal records'):
            package.verify(self.exe)

    def test_source_verification_rejects_stale_nested_bytecode(self):
        for name in package.REQUIRED_MODULES:
            text = 'marker = 1'
            if name == 'slg_gui':
                text = 'APP_VERSION="0.26.0"\nTEST_APP_VERSION="0.26.0"'
            if name == 'slg_source_status':
                text = 'def nested():\n    return "current"'
            (self.root / (name + '.py')).write_text(text, encoding='utf-8')
            self.code[name] = compile(text, 'bundled.py', 'exec')
        self.assertTrue(all(package.verify(self.exe, source_root=self.root)['source_matches'].values()))
        self.code['slg_source_status'] = compile('def nested():\n    return "stale"', 'bundled.py', 'exec')
        with self.assertRaisesRegex(ValueError, 'slg_source_status'):
            package.verify(self.exe, source_root=self.root)
