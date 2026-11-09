"""Outside facts: Git, Rex panes, Reason. Tests replace World with a fake."""
import json
import os
import shutil
import subprocess
from pathlib import Path

PREFIX = "rex-agents:"


def label_name(label):
    """Worker name from a rex-agents pane label, else None."""
    if not isinstance(label, str) or not label.startswith(PREFIX):
        return None
    _, _, name = label[len(PREFIX):].partition(":")
    return name or None


class World:
    def __init__(self, cwd=None, env=None):
        self.cwd = cwd or os.getcwd()
        self.env = os.environ if env is None else env

    # identity
    def block(self):
        return self.env.get("REX_BLOCK")

    def panes(self):
        """{block_id: label} across Rex sessions, or None when Rex cannot answer."""
        try:
            sessions = json.loads(_run(["rex", "ls", "--json"]))
            sessions = sessions.get("sessions", []) if isinstance(sessions, dict) else sessions
            found = {}
            for s in sessions:
                data = json.loads(_run(["rex", "block", "ls", "-s", s["session_id"], "--json"]))
                for b in data.get("blocks", []):
                    found[b["block_id"]] = b.get("label")
            return found
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return None

    def reason_address(self):
        try:
            data = json.loads(_run([_reason(), "who", "--json"]))
            return next((s["address"] for s in data.get("sessions", []) if s.get("is_me")), None)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def ring(self, to, text, fyi=False, notify=False):
        argv = [_reason(), "send"] + (["--fyi"] if fyi else []) + (["--notify"] if notify else []) + [to, text]
        try:
            _run(argv)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)

    # git
    def git(self, *args, cwd=None):
        """Stdout, or None when git fails."""
        try:
            return _run(["git", *args], cwd=cwd or self.cwd).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def common_dir(self):
        d = self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(d) if d else None

    def subject(self, rev, base, cwd=None):
        """Commit, tree, and merge base of rev; None fields when unknown."""
        commit = self.git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=cwd)
        if not commit:
            return None
        return {"commit": commit,
                "tree": self.git("rev-parse", f"{commit}^{{tree}}", cwd=cwd),
                "base": self.git("merge-base", commit, base, cwd=cwd) if base else None}

    def clean(self, cwd):
        out = self.git("status", "--porcelain", "--untracked-files=all", cwd=cwd)
        return out == ""

    def branch(self, cwd):
        return self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)

    def run(self, argv, cwd, log_path):
        with open(log_path, "wb") as log:
            try:
                return subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT).returncode
            except OSError as exc:
                log.write(f"{exc}\n".encode())
                return 127


def _reason():
    return shutil.which("reason-network") or str(Path.home() / ".reason/bin/reason-network")


def _run(argv, cwd=None):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise subprocess.SubprocessError((r.stderr or r.stdout).strip() or f"{argv[0]} exited {r.returncode}")
    return r.stdout
