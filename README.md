# ledger

A per-repo state machine for a driver agent (chief of staff) and its long-lived leads. Agents append events; the CLI checks each event's guard under a lock and computes every view from the log plus a git snapshot. It needs only git and Python 3.11+: no terminal multiplexer, launcher, or message bus.

- Store: append-only `events.jsonl` under `git rev-parse --git-common-dir`/ledger.
- Identity: the caller's agent process (`claude` or `codex`, by pid and start time), so it survives context compaction and `/clear`. `LEDGER_SESSION` overrides it.
- Lifecycle: `open` → `closed` | `dropped`. Progress, blockers, and agent health are computed.
- Proofs run on a clean, committed worktree; the driver accepts or rejects each one by ID.
- Inbox: open requests (asks, proofs to review, ideas, human decisions). `ledger next` returns one action; `ledger next --wait` blocks until there is one.

```sh
ledger init [--launch '<template>'] [--doorbell reason]   # driver
ledger new <slug> "<title>" --req "tests:lead:tree:tests pass" [--merge]
ledger assign <slug> <lead> --branch <b> --cwd <worktree>  # writes a brief with a one-time token
ledger start <slug> --token <token>                         # lead, from its brief
ledger prove <slug> tests -- python3 -m unittest
ledger next --wait                                          # anyone: the one next action
ledger inbox --human                                        # the human decision queue
ledger decision <id> <option> --human --why "<reason>"
```

## Hosts

The ledger runs anywhere an agent has a shell. Two optional settings adapt it to a host:

- `--launch` is a shell template that `assign` runs to start the agent. Fields: `{name} {role} {task} {cwd} {brief} {log}`. Without it, `assign` tells the driver where the brief is.
  - Headless: `claude -p --model haiku --dangerously-skip-permissions "$(cat {brief})" > {log} 2>&1`
  - rex-agents: `rex-agents launch --tab --name {name} --cwd {cwd} --model claude-opus-5-5 --effort medium --task-file {brief}`
- `--doorbell reason` wakes agents through `reason-network`. Without it, agents wait with `ledger next --wait`.

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
