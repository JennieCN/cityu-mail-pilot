"""Hermetic delivery and timing checks; no network or production."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tools import task_delivery as d


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for args in (['init', '-q'], ['config', 'user.name', 'Test'], ['config', 'user.email', 'test@example.invalid']):
            d.git(self.root, *args)
        (self.root/'code.py').write_text('x=1\n')
        d.git(self.root, 'add', 'code.py')
        d.git(self.root, 'commit', '-qm', 'fixture')
        self.head = d.git(self.root, 'rev-parse', 'HEAD').decode().strip()

    def test_roundtrip_and_git_unchanged(self):
        (self.root/'code.py').write_text('x=2\n')
        (self.root/'new.py').write_text('y=3\n')
        (self.root/'private.env').write_text('excluded')
        index = (self.root/'.git/index').read_bytes()
        dest = d.package(self.root, self.head, ['code.py','new.py'], [])
        manifest = json.loads((dest/'manifest.json').read_text())
        self.assertFalse(manifest['acceptance'])
        self.assertNotIn('private.env', manifest['files'])
        self.assertEqual(index, (self.root/'.git/index').read_bytes())
        self.assertEqual(d.sha((dest/'changes.patch').read_bytes()), manifest['artifacts']['changes.patch'])
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target/'code.py').write_text('x=1\n')
            subprocess.run(['git','apply',str(dest/'changes.patch')], cwd=target, check=True)
            self.assertEqual((target/'code.py').read_text(),'x=2\n')
            self.assertEqual((target/'new.py').read_text(),'y=3\n')
        second = d.package(self.root, self.head, ['code.py','new.py'], [])
        self.assertNotEqual(dest,second)
        self.assertEqual((dest/'changes.patch').read_bytes(),(second/'changes.patch').read_bytes())
        self.assertTrue(d.verify(dest)['integrity'])
        (dest/'changes.patch').write_bytes(b'altered')
        with self.assertRaises(ValueError):
            d.verify(dest)

    def test_deletion(self):
        (self.root/'code.py').unlink()
        dest=d.package(self.root,self.head,['code.py'],[])
        self.assertIsNone(json.loads((dest/'manifest.json').read_text())['files']['code.py'])

    def test_scope_and_baseline(self):
        (self.root/'code.py').write_text('changed')
        for head, files in [(self.head,['other.py']),('0'*40,['code.py']),(self.head,[])]:
            with self.assertRaises(ValueError):
                d.package(self.root,head,files,[])
        self.assertFalse((self.root/'.task-delivery').exists())

    def test_staged_deletion(self):
        d.git(self.root,'rm','-q','code.py')
        dest=d.package(self.root,self.head,['code.py'],[])
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)
            (target/'code.py').write_text('x=1\n')
            subprocess.run(['git','apply',str(dest/'changes.patch')],cwd=target,check=True)
            self.assertFalse((target/'code.py').exists())

    def test_removed_from_index_only(self):
        d.git(self.root,'rm','--cached','-q','code.py')
        with self.assertRaises(ValueError):
            d.package(self.root,self.head,['code.py'],[])

    def test_path_rejection(self):
        (self.root/'link').symlink_to(self.root.parent, target_is_directory=True)
        for rel in ['../outside','/tmp/x','link/x',':(glob)*','-x','.git/../x','.git/x','./new.py','foo//bar']:
            with self.assertRaises(ValueError):
                d.safe(self.root,rel)

    def test_receipts_and_output_symlink(self):
        (self.root/'receipt.json').write_text('{"exit_code": 1}')
        dest=d.package(self.root,self.head,['code.py'],['receipt.json'])
        self.assertEqual(json.loads((dest/'receipt-0.json').read_text())['exit_code'],1)
        (self.root/'escape').symlink_to(self.root.parent,target_is_directory=True)
        with self.assertRaises(ValueError):
            d.package(self.root,self.head,['code.py'],[],'escape')

    def test_phase_failure_and_no_argv_persisted(self):
        self.assertEqual(d.phase(self.root,'tests',[sys.executable,'-c','raise SystemExit(7)']),7)
        folder=next((self.root/'.task-delivery').iterdir())
        start=json.loads((folder/'start.json').read_text())
        end=json.loads((folder/'end.json').read_text())
        self.assertGreaterEqual(end['monotonic_ns'],start['monotonic_ns'])
        self.assertEqual(end['exit_code'],7)
        self.assertNotIn('argv',start)

    def test_interrupted_and_failed_spawn(self):
        with patch.object(subprocess,'call',side_effect=KeyboardInterrupt):
            self.assertEqual(d.phase(self.root,'analysis',['example']),130)
        with patch.object(subprocess,'call',side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                d.phase(self.root,'waiting',['example'])
        records=[json.loads(p.read_text()) for p in (self.root/'.task-delivery').glob('*/end.json')]
        self.assertEqual({r['status'] for r in records},{'interrupted','failed_to_start'})


if __name__ == '__main__':
    unittest.main()
