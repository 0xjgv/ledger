"""Outside facts: git, the caller's agent process, and optional host hooks. Tests replace World with a fake."""
import os
import shlex
import subprocess
from pathlib import Path

# Agent harness executables whose process anchors a caller's identity.
HARNESSES = {"claude", "codex"}


class World:
    def __init__(self, cwd=None, env=None):
        self.cwd = cwd or os.getcwd()
        self.env = os.environ if env is None else env

    # --- identity
    def sessions(self):
        """Identity keys for the caller, strongest first.

        LEDGER_SESSION overrides; otherwise the nearest agent harness process (pid + start time),
        which survives context compaction and /clear."""
        keys = []
        if self.env.get("LEDGER_SESSION"):
            keys.append("env:" + self.env["LEDGER_SESSION"])
        proc = self.harness()
        if proc:
            keys.append(proc)
        return keys

    def harness(self):
        """proc:<pid>:<start> of the nearest ancestor named like an agent harness, else None."""
        names = HARNESSES | set(filter(None, self.env.get("LEDGER_HARNESS", "").split(",")))
        pid = os.getppid()
        for _ in range(12):
            info = _ps(pid)
            if not info:
                return None
            ppid, start, comm = info
            if Path(comm).name in names:
                return f"proc:{pid}:{start}"
            if ppid <= 1:
                return None
            pid = ppid
        return None

    def alive(self, session):
        """True or False for a process session; None when this host cannot tell."""
        if not session or not session.startswith("proc:"):
            return None
        _, pid, start = session.split(":", 2)
        info = _ps(int(pid))
        return bool(info) and info[1] == start

    # --- host hooks (configured per ledger)
    def spawn(self, command, log_path, status_path):
        """Run a launch command detached. The status file receives its exit code when it ends."""
        script = f"{command}; echo $? > {shlex.quote(str(status_path))}"
        with open(log_path, "ab") as log:
            p = subprocess.Popen(["/bin/sh", "-c", script], stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, cwd=self.cwd)
        return p.pid

    def shell(self, command):
        """(ok, stdout or error) for a configured shell command."""
        try:
            return True, _run(["/bin/sh", "-c", command], cwd=self.cwd).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    # --- git
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
        """Commit, tree, and merge base of rev; None when rev has no commit."""
        commit = self.git("rev-parse", "--verify", f"{rev}^{{commit}}", cwd=cwd)
        if not commit:
            return None
        return {"commit": commit,
                "tree": self.git("rev-parse", f"{commit}^{{tree}}", cwd=cwd),
                "base": self.git("merge-base", commit, base, cwd=cwd) if base else None}

    def clean(self, cwd):
        return self.git("status", "--porcelain", "--untracked-files=all", cwd=cwd) == ""

    def branch(self, cwd):
        return self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)

    def run(self, argv, cwd, log_path):
        with open(log_path, "wb") as log:
            try:
                return subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT).returncode
            except OSError as exc:
                log.write(f"{exc}\n".encode())
                return 127


def _ps(pid):
    """(ppid, start, command) for pid, or None."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = out.split()
    # lstart is five fields: Mon Nov  9 12:21:01 2026
    return int(parts[0]), " ".join(parts[1:6]), " ".join(parts[6:])


def _run(argv, cwd=None):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise subprocess.SubprocessError((r.stderr or r.stdout).strip() or f"{argv[0]} exited {r.returncode}")
    return r.stdout
