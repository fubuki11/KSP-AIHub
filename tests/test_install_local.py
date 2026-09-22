import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe')


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell required')
class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='AI Hub install ')
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.root = self.workspace / 'source'
        (self.root / 'scripts').mkdir(parents=True)
        for name in ('install-local.ps1', 'configure.ps1'):
            shutil.copyfile(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        version = {'VERSION': {'MAJOR': 0, 'MINOR': 1, 'PATCH': 2}, 'KSP_VERSION': {'MAJOR': 1, 'MINOR': 12, 'PATCH': 5}}
        version_file = self.root / 'GameData/KSPAIHub/KSPAIHub.version'
        version_file.parent.mkdir(parents=True); version_file.write_text(json.dumps(version))
        self.dist = self.root / 'dist'; self.dist.mkdir()
        self.archive = self.dist / 'KSPAIHub-0.1.2.zip'
        self.payload = {'GameData/KSPAIHub/Plugins/KSPAIHub.dll': b'MZ-fixture-dll',
                        'GameData/KSPAIHub/KSPAIHub.version': json.dumps(version).encode(),
                        'GameData/KSPAIHub/Service/ksp_aihub/__main__.py': b'# fixture service',
                        'GameData/KSPAIHub/hub.example.json': b'{"schemaVersion":1,"profiles":{}}',
                        'GameData/KSPAIHub/README.md': b'fixture documentation'}
        metadata = {'identifier': 'KSPAIHub', 'version': '0.1.2', 'download': self.archive.as_uri(),
                    'install': [{'file': 'GameData/KSPAIHub', 'install_to': 'GameData'}]}
        self.metadata = self.dist / 'KSPAIHub-0.1.2.ckan'
        self.metadata.write_text(json.dumps(metadata))
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, content in self.payload.items(): archive.writestr(name, content)
            archive.writestr('KSPAIHub.ckan', json.dumps(metadata))
        with zipfile.ZipFile(self.dist / 'KSPAIHub-local-repository.zip', 'w') as archive:
            archive.writestr('KSPAIHub/KSPAIHub-0.1.2.ckan', json.dumps(metadata))
        self.game = self.workspace / 'game'
        (self.game / 'GameData').mkdir(parents=True); (self.game / 'CKAN').mkdir()
        self.registry = self.game / 'CKAN/registry.json'
        self.registry.write_text(json.dumps({'sorted_repositories': {}, 'installed_modules': {}, 'installed_files': {}}))
        self.log = self.workspace / 'commands.jsonl'
        stub = self.workspace / 'ckan.py'
        stub.write_text('''import json,os,sys,zipfile
from pathlib import Path
a=sys.argv[1:]
with Path(os.environ['HUB_TEST_LOG']).open('a') as f: f.write(json.dumps(a)+'\\n')
mode=os.environ.get('HUB_TEST_MODE','success')
if mode=='fail':sys.exit(37)
game=Path(a[a.index('--gamedir')+1]); path=game/'CKAN/registry.json'; reg=json.loads(path.read_text())
if a[0]=='repo':
    repos=reg.setdefault('sorted_repositories',{})
    if a[2] in repos:sys.exit(23)
    repos[a[2]]={'uri':a[3]}
elif a[0]=='update':sys.exit(0)
elif a[0] in ('install','upgrade'):
    if mode=='empty':sys.exit(0)
    with zipfile.ZipFile(os.environ['HUB_TEST_ARCHIVE']) as z:
        meta=json.loads(z.read('KSPAIHub.ckan')); names=[n for n in z.namelist() if n.startswith('GameData/')]
        if mode!='registration-only':
            for name in names:
                output=game/name; output.parent.mkdir(parents=True,exist_ok=True); output.write_bytes(z.read(name))
    reg['installed_modules']['KSPAIHub']={'source_module':meta,'installed_files':{n:{} for n in names}}
    reg['installed_files'].update({n:'KSPAIHub' for n in names})
path.write_text(json.dumps(reg))
''', encoding='utf-8')
        self.ckan = self.workspace / 'ckan.cmd'
        self.ckan.write_text(f'@echo off\n"{sys.executable}" "{stub}" %*\nexit /b %errorlevel%\n')
        self.env = dict(os.environ, HUB_TEST_LOG=str(self.log), HUB_TEST_ARCHIVE=str(self.archive))

    def run_install(self):
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-File', str(self.root/'scripts/install-local.ps1'),
            '-KspRoot', str(self.game), '-CkanPath', str(self.ckan)], env=self.env, cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='oem', errors='replace', timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_cached_archive_is_not_installation_and_pipeline_registers_exact_version(self):
        (self.game/'CKAN/cached.zip').write_bytes(self.archive.read_bytes())
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c[0] for c in self.calls()], ['repo', 'update', 'install'])
        self.assertIn('KSPAIHub=0.1.2', self.calls()[-1])
        self.assertIn('--urls', self.calls()[1])
        for name, content in self.payload.items(): self.assertEqual((self.game/name).read_bytes(), content)
        self.assertTrue((self.game/'GameData/KSPAIHub/PluginData/launcher.json').exists())

    def test_zero_exit_without_registration_is_rejected(self):
        self.env['HUB_TEST_MODE'] = 'empty'
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not register', result.stdout)
        self.assertFalse((self.game/'GameData/KSPAIHub/PluginData/launcher.json').exists())

    def test_registration_without_files_is_rejected(self):
        self.env['HUB_TEST_MODE'] = 'registration-only'
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('file missing', result.stdout)

    def test_repeated_install_preserves_launcher_and_does_not_reinstall(self):
        self.assertEqual(self.run_install().returncode, 0)
        path = self.game/'GameData/KSPAIHub/PluginData/launcher.json'
        path.write_text('{"user":"custom preferences"}')
        before = path.read_bytes(); self.log.unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c[0] for c in self.calls()], ['update'])
        self.assertEqual(path.read_bytes(), before)

    def test_registered_modified_payload_is_not_overwritten(self):
        self.assertEqual(self.run_install().returncode, 0)
        path = self.game/'GameData/KSPAIHub/Service/ksp_aihub/__main__.py'
        path.write_text('user change'); self.log.unlink()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('payload differs', result.stdout)
        self.assertFalse(self.calls())
        self.assertEqual(path.read_text(), 'user change')

    def test_upgrade_uses_indexed_explicit_version(self):
        self.assertEqual(self.run_install().returncode, 0)
        reg = json.loads(self.registry.read_text()); reg['installed_modules']['KSPAIHub']['source_module']['version'] = '0.1.1'
        self.registry.write_text(json.dumps(reg)); self.log.unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c[0] for c in self.calls()], ['update', 'upgrade'])
        self.assertIn('KSPAIHub=0.1.2', self.calls()[-1])
        self.assertNotIn('--ckanfile', self.calls()[-1])

    def test_conflicting_repository_location_is_preserved(self):
        reg = json.loads(self.registry.read_text()); reg['sorted_repositories']['KSPAIHub-local'] = {'uri':'file:///another/repository.zip'}
        self.registry.write_text(json.dumps(reg)); before=self.registry.read_bytes()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another location', result.stdout)
        self.assertEqual(before, self.registry.read_bytes()); self.assertFalse(self.calls())

    def test_ckan_failure_stops_subsequent_steps(self):
        self.env['HUB_TEST_MODE'] = 'fail'
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exit code 37', result.stdout)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.game/'GameData/KSPAIHub/PluginData').exists())

    def test_registry_lock_is_respected(self):
        path = self.game/'CKAN/registry.locked'; path.write_bytes(b'lock')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle=kernel.CreateFileW(str(path),0x80000000,0,None,3,0,None)
        self.assertNotEqual(handle,wintypes.HANDLE(-1).value)
        try: result=self.run_install()
        finally: kernel.CloseHandle(handle)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Exit its GUI normally',result.stdout); self.assertFalse(self.calls())
        self.assertEqual(path.read_bytes(),b'lock')


if __name__ == '__main__': unittest.main()
