# ledger

A per-repo state machine for a driver agent (chief of staff) and its long-lived leads. Agents append events; the CLI checks each event's guard under a lock and computes every view from the log plus a Git and Rex snapshot. Reason rings the doorbell; the ledger holds the record.

- Store: append-only `events.jsonl` under `git rev-parse --git-common-dir`/ledger.
- Lifecycle: `open` → `closed` | `dropped`. Progress, blockers, and pane health are computed.
- Proofs run on a clean, committed worktree; the driver accepts or rejects each one by ID.
- Inbox: open requests (asks, proofs to review, ideas, human decisions). `ledger next` returns one action.
- v1 supports Claude Code panes launched by rex-agents. Python 3.11+, standard library only.

```sh
ledger init                      # driver
ledger new <slug> "<title>" --req "tests:lead:tree:tests pass" [--merge]
ledger assign <slug> <lead> --branch <b> --cwd <worktree>
ledger start <slug>              # lead
ledger prove <slug> tests -- bun run check
ledger next                      # anyone: the one next action
ledger inbox --human             # the human decision queue
ledger decision <id> <option> --human --why "<reason>"
```

## Install

Needs Python 3.11 or newer.

```sh
curl -fsSL https://github.com/0xjgv/ledger/releases/latest/download/install.sh | sh
```

It verifies the release archive's SHA-256, unpacks it under `~/.local/share/ledger`, and links `~/.local/bin/ledger`. Pin a version with `LEDGER_VERSION=v0.1.0`.

Update in place: `ledger update` (or `ledger update v0.2.0`). It checks the release's `install.sh` against `checksums.txt`, then runs it.

## Develop

```sh
make test               # unit and installer tests
make package            # dist/: archive, install.sh, checksums.txt
make release BUMP=minor # bump, commit, tag, push; CI publishes the release
```
