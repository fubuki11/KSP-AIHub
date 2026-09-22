"""Public download metadata is tested without game assemblies or network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class PublicBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aihub public build ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        files = ["scripts/build.ps1", "src/KSPAIHub/KSPAIHub.csproj", "GameData/KSPAIHub/KSPAIHub.version",
                 "README.md", "DESIGN.md", "PROTOCOL.md", "INSTALL.md", "MODELS.md", "GENERATION.md", "LICENSE", "examples/hub.example.json"]
        files += [str(p.relative_to(ROOT)) for p in (ROOT / "service/ksp_aihub").glob("*.py")]
        for name in files:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        binary = self.root / "src/KSPAIHub/bin/Release/net472/KSPAIHub.dll"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"isolated build fixture, not a game DLL")
        tools = Path(temporary.name) / "tools"
        tools.mkdir()
        (tools / "dotnet.cmd").write_text('@echo off\nexit /b 0\n')
        self.env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""))
        data = json.loads((ROOT / "GameData/KSPAIHub/KSPAIHub.version").read_text())["VERSION"]
        self.version = ".".join(str(data[k]) for k in ("MAJOR", "MINOR", "PATCH"))

    def build(self, *extra):
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(self.root / "scripts/build.ps1"),
                               "-KspRoot", str(self.root), *extra], env=self.env, cwd=self.root,
                              capture_output=True, text=True, encoding="oem", errors="replace", timeout=30)

    def test_public_url_is_identical_in_all_three_metadata_copies(self):
        url = f"https://github.com/fubuki11/KSP-AIHub/releases/download/v{self.version}/KSPAIHub-{self.version}.zip"
        result = self.build("-DownloadUrl", url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((self.root / f"dist/KSPAIHub-{self.version}.ckan").read_text())
        self.assertEqual(metadata["download"], url)
        with zipfile.ZipFile(self.root / f"dist/KSPAIHub-{self.version}.zip") as archive:
            self.assertEqual(json.loads(archive.read("KSPAIHub.ckan")), metadata)
        with zipfile.ZipFile(self.root / "dist/KSPAIHub-local-repository.zip") as archive:
            self.assertEqual(json.loads(archive.read(f"KSPAIHub/KSPAIHub-{self.version}.ckan")), metadata)

    def test_default_remains_local_and_invalid_urls_do_not_replace_outputs(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((self.root / f"dist/KSPAIHub-{self.version}.ckan").read_text())
        self.assertEqual(metadata["download"], (self.root / f"dist/KSPAIHub-{self.version}.zip").as_uri())
        before = {p.name: p.read_bytes() for p in (self.root / "dist").iterdir()}
        for url in ("http://example.org/file.zip", "https://user:secret@example.org/file.zip", "https://example.org/file.zip?token=secret", "https://example.org/file.zip#fragment"):
            result = self.build("-DownloadUrl", url)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root / "dist").iterdir()})


if __name__ == "__main__": unittest.main()
