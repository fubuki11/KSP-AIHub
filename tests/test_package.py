import json
from pathlib import Path
import struct
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = json.loads((ROOT / 'GameData/KSPAIHub/KSPAIHub.version').read_text())['VERSION']
        cls.version = '.'.join(str(value[k]) for k in ('MAJOR', 'MINOR', 'PATCH'))
        cls.archive = ROOT / f'dist/KSPAIHub-{cls.version}.zip'
        if not cls.archive.exists(): raise unittest.SkipTest('Build the gateway package first')

    def test_archive_contains_only_hub_payload_and_no_runtime_secrets(self):
        with zipfile.ZipFile(self.archive) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIsNone(archive.testzip())
            self.assertIn('GameData/KSPAIHub/Plugins/KSPAIHub.dll', names)
            self.assertIn('GameData/KSPAIHub/Service/ksp_aihub/auth.py', names)
            self.assertIn('GameData/KSPAIHub/Service/ksp_aihub/models.py', names)
            self.assertIn('GameData/KSPAIHub/Service/ksp_aihub/presets.py', names)
            self.assertIn('GameData/KSPAIHub/Service/ksp_aihub/diagnostics.py', names)
            self.assertIn('GameData/KSPAIHub/DIAGNOSTICS.md', names)
            self.assertIn('GameData/KSPAIHub/MODELS.md', names)
            for name in names:
                self.assertTrue(name == 'KSPAIHub.ckan' or name.startswith('GameData/KSPAIHub/'))
                self.assertNotIn('..', name)
                self.assertNotIn('\\', name)
                self.assertNotIn('PluginData', name)
                self.assertNotIn('connection.json', name)
                self.assertNotIn('hub.local.json', name)
                self.assertFalse(name.endswith('.dpapi.json'))
            self.assertEqual([n for n in names if n.endswith('.dll')], ['GameData/KSPAIHub/Plugins/KSPAIHub.dll'])

    def test_version_metadata_repo_and_binary_agree(self):
        metadata = json.loads((ROOT / f'dist/KSPAIHub-{self.version}.ckan').read_text())
        self.assertEqual(metadata['identifier'], 'KSPAIHub')
        self.assertEqual(metadata['version'], self.version)
        self.assertEqual(metadata['install'], [{'file': 'GameData/KSPAIHub', 'install_to': 'GameData'}])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(json.loads(archive.read('KSPAIHub.ckan')), metadata)
            binary = archive.read('GameData/KSPAIHub/Plugins/KSPAIHub.dll')
            self.assertEqual(binary[:2], b'MZ')
            offset = struct.unpack_from('<I', binary, 0x3c)[0]
            self.assertEqual(binary[offset:offset+4], b'PE\x00\x00')
        with zipfile.ZipFile(ROOT / 'dist/KSPAIHub-local-repository.zip') as archive:
            self.assertEqual(archive.namelist(), [f'KSPAIHub/KSPAIHub-{self.version}.ckan'])
            self.assertEqual(json.loads(archive.read(archive.namelist()[0])), metadata)


if __name__ == '__main__': unittest.main()
