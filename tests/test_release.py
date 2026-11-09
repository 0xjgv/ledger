import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "ledger/__init__.py").read_text().split('"')[1]


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dist = self.tmp / "dist"
        subprocess.run(["sh", str(ROOT / "scripts/package.sh"), str(self.dist)], check=True, capture_output=True)
        self.env = dict(os.environ, LEDGER_DOWNLOAD_BASE=str(self.dist), LEDGER_HOME=str(self.tmp / "home"),
                        LEDGER_BIN_DIR=str(self.tmp / "bin"))
        self.env.pop("LEDGER_VERSION", None)

    def run_sh(self, *argv):
        return subprocess.run(list(argv), env=self.env, capture_output=True, text=True)

    def test_install_then_version(self):
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_sh(str(self.tmp / "bin/ledger"), "version").stdout.strip(), VERSION)

    def test_tampered_archive_refused(self):
        archive = self.dist / f"ledger-{VERSION}.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"x")
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)

    def test_pinned_version_must_exist(self):
        self.env["LEDGER_VERSION"] = "v9.9.9"
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertIn("expected one ledger archive", r.stderr)

    def test_update_reinstalls_and_rejects_tampered_installer(self):
        self.run_sh("sh", str(self.dist / "install.sh"))
        installed = str(self.tmp / "bin/ledger")
        r = self.run_sh(installed, "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Installed {self.tmp / 'bin/ledger'}: {VERSION}", r.stdout)
        (self.dist / "install.sh").write_text("echo pwned\n")
        r = self.run_sh(installed, "update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match checksums.txt", r.stderr)
        self.assertEqual(self.run_sh(installed, "update", "nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()
