# ledger

> **Deprecated: frozen at v0.6.0.** ledger now ships inside [cos](https://github.com/0xjgv/cos) (private), the chief of staff's tools. Installing cos installs both the `cos` and `ledger` commands; `cos ledger <verb>` does the same thing. Existing stores (`events.jsonl`) work unchanged. This repo and its releases stay up, and v0.6.0 stays installable, but nothing new lands here. Do not install both on one machine: this repo's `install.sh` and `ledger update` reinstall standalone ledger and take the `ledger` command back from cos.

A per-repo record of work for a driver agent (chief of staff) and its leads: tasks, who leads them, their state, messages, and one inbox. It records facts and never acts: no launching, waking, or verifying agents. It needs only git and Python 3.11+ (the installer gets Python through uv).

- Store: append-only `events.jsonl` under `git rev-parse --git-common-dir`/ledger, written under a lock. `LEDGER_DIR=/abs/dir` uses that directory instead, from any cwd; its parent must exist.
- Names: `--as <name>` or `LEDGER_AS`; the default is `driver`. Nothing checks them.
- States: `todo → active → review → done`, plus `blocked` and `dropped`. `ledger set` refuses other moves; setting the current state again is allowed.
- Inbox: `ask` opens an item for a name (`driver` by default, `human` for you); `answer` closes it, and the answer shows once in the asker's inbox. Ending a task closes its items.

```sh
ledger new login "Fix login" --lead ana             # driver
ledger set login state=active branch=fix-login      # any field; key= clears it
ledger say login "root cause: stale cookie"         # message on the task
ledger ask login "Postgres or SQLite?" --wait       # blocks until answered (100s default)
ledger ask - "Merge login?" --to human --option "merge: ship it" --option hold
ledger ask login "CI red on abc123" --key ci:login:abc123  # returns the open item with this key, if any
ledger answer 4 "merge"                             # --as human
ledger inbox [--for human | --all] [--wait [--after SEQ]]  # ends with "head: SEQ"; --after waits past it
ledger board [--all]
ledger show login | ledger show 4
ledger stats [--where sha=abc123 ...]               # cycle time, time in state, reworks, blocks, answer waits
ledger recall [text] [--tag decision ...]           # tagged notes by task; default decision, lesson, assumption
```

Tag a note that should outlive the task by starting it with `decision:`, `lesson:` or `assumption:`. `recall` reads only tagged notes, so untagged progress messages stay in `show` and out of recall.

`ask --key KEY` makes a retry safe: under the write lock, if an open item has the key, `ask` prints that item (`#4 asked driver (already open, key ...)`) instead of opening a new one and exits 0; `--json` adds `"existing": true` (`false` when it opened one). The open item must have the same task and recipient, else `ask` refuses with `key_conflict`. Once that item is answered or closed, the key opens a new item, so put recurrence in the key (a SHA, a date). Keys are opaque: 1-128 of `A-Z a-z 0-9 . _ : / @ # + = -`, starting with a letter or digit. `show #n` and `--json` include the key.

Re-arm a wait without a gap: pass the last `head:` (or `board --json`'s `"head"`) as `--after`; old open items no longer wake you, new ones and unseen answers do.

Add `--json` to any command for machine output. Logs from 0.1 and 0.2 still load: tasks, leads, states, and requests carry over; proofs are dropped.

## Install

Needs [uv](https://docs.astral.sh/uv/); uv downloads Python 3.11+ if none is installed.

```sh
curl -fsSL https://github.com/0xjgv/ledger/releases/latest/download/install.sh | sh
```

It verifies the release wheel's SHA-256, keeps it under `~/.local/share/ledger/releases`, and installs it with `uv tool install`; the `ledger` command lands in `uv tool dir --bin` (usually `~/.local/bin`). Pin a version with `LEDGER_VERSION=v0.3.0`. It replaces the unpacked layout older releases used. Uninstall with `uv tool uninstall ledger`.

Update in place: `ledger update` (or `ledger update v0.3.0`). It checks the release's `install.sh` against `checksums.txt`, then runs it.

## Develop

```sh
make test               # unit and installer tests
make package            # dist/: wheel, install.sh, checksums.txt
make release BUMP=minor # bump, commit, tag, push; CI publishes the release
```
