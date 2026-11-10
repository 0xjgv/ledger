import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "ledger/__init__.py").read_text().split('"')[1]
WHEEL = f"ledger-{VERSION}-py3-none-any.whl"


class Release(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = Path(tempfile.mkdtemp()) / "dist"
        subprocess.run(["sh", str(ROOT / "scripts/package.sh"), str(cls.built)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.built.parent)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dist = self.tmp / "dist"
        shutil.copytree(self.built, self.dist)
        self.bin = self.tmp / "bin/ledger"
        self.env = dict(os.environ, LEDGER_DOWNLOAD_BASE=str(self.dist), LEDGER_HOME=str(self.tmp / "home"),
                        LEDGER_BIN_DIR=str(self.tmp / "bin"), UV_TOOL_DIR=str(self.tmp / "tools"))
        self.env.pop("LEDGER_VERSION", None)

    def run_sh(self, *argv):
        return subprocess.run(list(argv), env=self.env, capture_output=True, text=True)

    def install(self):
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_install_then_version(self):
        self.install()
        self.assertEqual(self.run_sh(str(self.bin), "version").stdout.strip(), VERSION)
        self.assertTrue((self.tmp / "home/releases" / WHEEL).is_file())

    def test_tampered_wheel_refused(self):
        wheel = self.dist / WHEEL
        wheel.write_bytes(wheel.read_bytes() + b"x")
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)

    def test_pinned_version_must_exist(self):
        self.env["LEDGER_VERSION"] = "v9.9.9"
        r = self.run_sh("sh", str(self.dist / "install.sh"))
        self.assertIn("expected one ledger wheel", r.stderr)

    def test_install_replaces_the_unpacked_layout_of_older_releases(self):
        old = self.tmp / "home/ledger-0.3.0/bin/ledger"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\necho 0.3.0\n")
        old.chmod(0o755)
        self.bin.parent.mkdir()
        self.bin.symlink_to(old)
        self.install()
        self.assertFalse(self.bin.is_symlink() and self.bin.resolve() == old)
        self.assertFalse(old.parent.parent.exists())
        self.assertEqual(self.run_sh(str(self.bin), "version").stdout.strip(), VERSION)

    def test_update_reinstalls_and_rejects_tampered_installer(self):
        self.install()
        r = self.run_sh(str(self.bin), "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Installed {self.bin}: {VERSION}", r.stdout)
        (self.dist / "install.sh").write_text("echo pwned\n")
        r = self.run_sh(str(self.bin), "update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match checksums.txt", r.stderr)
        self.assertEqual(self.run_sh(str(self.bin), "update", "nope").returncode, 2)

    def test_update_refuses_to_downgrade_unless_pinned(self):
        self.install()
        init, = (self.tmp / "tools/ledger").glob("lib/python*/site-packages/ledger/__init__.py")
        init.write_text('__version__ = "10.0.0"\n')  # new length, so the cached .pyc is stale
        r = self.run_sh(str(self.bin), "update")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"latest is {VERSION}, you have 10.0.0", r.stderr)
        r = self.run_sh(str(self.bin), "update", f"v{VERSION}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_sh(str(self.bin), "version").stdout.strip(), VERSION)


if __name__ == "__main__":
    unittest.main()
