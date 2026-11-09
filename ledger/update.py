"""ledger update: run the release's own install.sh after checking its digest."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import __version__

REPO = "0xjgv/ledger"


def release_url(version):
    base = os.environ.get("LEDGER_DOWNLOAD_BASE", f"https://github.com/{REPO}/releases").rstrip("/")
    if base.startswith("/"):
        return base
    return f"{base}/download/v{version}" if version else f"{base}/latest/download"


def fetch(release, name):
    if release.startswith("/"):
        return Path(release, name).read_bytes()
    with urllib.request.urlopen(f"{release}/{name}", timeout=30) as resp:
        return resp.read()


def update(version=None):
    version = (version or "").removeprefix("v")
    release = release_url(version)
    try:
        sums = fetch(release, "checksums.txt").decode()
        script = fetch(release, "install.sh")
    except OSError as exc:
        print(f"ledger update: download failed from {release}: {exc}", file=sys.stderr)
        return 1
    expected = next((line.split()[0] for line in sums.splitlines()
                     if line.split()[1:] and line.split()[1].lstrip("*") == "install.sh"), None)
    if expected != hashlib.sha256(script).hexdigest():
        print("ledger update: install.sh does not match checksums.txt", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "install.sh")
        path.write_bytes(script)
        env = {k: v for k, v in os.environ.items() if k != "LEDGER_VERSION"}
        if version:
            env["LEDGER_VERSION"] = version
        print(f"ledger update: {__version__} -> {version or 'latest'}", file=sys.stderr)
        return subprocess.run([shutil.which("sh") or "/bin/sh", str(path)], env=env).returncode
