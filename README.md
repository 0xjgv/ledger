# ledger

A per-repo record of work for a driver agent (chief of staff) and its leads: tasks, who leads them, their state, messages, and one inbox. It records facts and never acts: no launching, waking, or verifying agents. It needs only git and Python 3.11+.

- Store: append-only `events.jsonl` under `git rev-parse --git-common-dir`/ledger, written under a lock.
- Names: `--as <name>` or `LEDGER_AS`; the default is `driver`. Nothing checks them.
- States: `todo → active → review → done`, plus `blocked` and `dropped`. `ledger set` refuses other moves.
- Inbox: `ask` opens an item for a name (`driver` by default, `human` for you); `answer` closes it, and the answer shows once in the asker's inbox. Ending a task closes its items.

```sh
ledger new login "Fix login" --lead ana             # driver
ledger set login state=active branch=fix-login      # any field; key= clears it
ledger say login "root cause: stale cookie"         # message on the task
ledger ask login "Postgres or SQLite?" --wait       # blocks until answered (100s default)
ledger ask - "Merge login?" --to human --option "merge: ship it" --option hold
ledger answer 4 "merge"                             # --as human
ledger inbox [--for human | --all] [--wait]
ledger board [--all]
ledger show login | ledger show 4
```

Add `--json` to any command for machine output. Logs from 0.1 and 0.2 still load: tasks, leads, states, and requests carry over; proofs are dropped.

## Install

Needs Python 3.11 or newer.

```sh
curl -fsSL https://github.com/0xjgv/ledger/releases/latest/download/install.sh | sh
```

It verifies the release archive's SHA-256, unpacks it under `~/.local/share/ledger`, and links `~/.local/bin/ledger`. Pin a version with `LEDGER_VERSION=v0.3.0`.

Update in place: `ledger update` (or `ledger update v0.3.0`). It checks the release's `install.sh` against `checksums.txt`, then runs it.

## Develop

```sh
make test               # unit and installer tests
make package            # dist/: archive, install.sh, checksums.txt
make release BUMP=minor # bump, commit, tag, push; CI publishes the release
```
