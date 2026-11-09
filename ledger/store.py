"""Append-only JSONL log with one lock file, fsync, and torn-tail repair."""
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from pathlib import Path


class Corrupt(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.log = self.root / "events.jsonl"
        self.evidence = self.root / "evidence"
        self.briefs = self.root / "briefs"

    def exists(self):
        return self.log.exists()

    def create(self):
        for d in (self.root, self.evidence, self.briefs):
            d.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        _fsync_dir(self.root)

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self):
        """(events, torn_tail_bytes). Raises Corrupt on a bad interior line or a sequence gap."""
        if not self.log.exists():
            return [], 0
        raw = self.log.read_bytes()
        lines = raw.split(b"\n")
        tail = lines.pop()  # bytes after the last newline; empty when the file ends cleanly
        events = []
        for n, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except ValueError:
                raise Corrupt(f"malformed line {n}") from None
            if event.get("seq") != len(events) + 1:
                raise Corrupt(f"sequence gap at line {n}")
            events.append(event)
        return events, len(tail)

    def append(self, events, torn, event):
        """Under the lock: drop a torn tail, then write one event with the next seq. Returns the event."""
        if torn:
            with open(self.log, "r+b") as fh:
                fh.truncate(self.log.stat().st_size - torn)
                os.fsync(fh.fileno())
            event = dict(event, repaired_torn_bytes=torn)
        event = {"seq": len(events) + 1, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
        with open(self.log, "ab") as fh:
            fh.write(json.dumps(event, sort_keys=True).encode() + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def keep(self, path):
        """Copy a file into content-addressed evidence; return its record."""
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        dest = self.evidence / (digest + Path(path).suffix)
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            shutil.copyfile(path, tmp)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        return {"name": Path(path).name, "sha256": digest, "path": str(dest)}


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
