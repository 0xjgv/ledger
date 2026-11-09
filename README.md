# ledger

A per-repo state machine for a driver agent (chief of staff) and its long-lived leads. Agents append events; the CLI checks each event's guard under a lock and computes every view from the log plus a git snapshot. It needs only git and Python 3.11+: no terminal multiplexer, launcher, or message bus.

- Store: append-only `events.jsonl` under `git rev-parse --git-common-dir`/ledger.
- Identity: the caller's agent process (`claude` or `codex`, by pid and start time), so it survives context compaction and `/clear`. `LEDGER_SESSION` overrides it.
- Lifecycle: `open` → `closed` | `dropped`. Progress, blockers, and agent health are computed.
- Proofs run on a clean, committed worktree; the driver accepts or rejects each one by ID.
- Inbox: open requests (asks, proofs to review, ideas, human decisions). `ledger next` returns one action; `ledger next --wait` blocks until there is one.

```sh
ledger init [--launch <cmd>] [--ring <cmd>] [--whoami <cmd>]  # driver
ledger new <slug> "<title>" --req "tests:lead:tree:tests pass" [--merge]
ledger assign <slug> <lead> --branch <b> --cwd <worktree>  # writes a brief with a one-time token
ledger start <slug> --token <token>                         # lead, from its brief
ledger prove <slug> tests -- python3 -m unittest
ledger next --wait                                          # anyone: the one next action
ledger inbox --human                                        # the human decision queue
ledger decision <id> <option> --human --why "<reason>"
```

## Hosts

The ledger contains no host-specific code. Three optional commands, set once with `ledger init`, adapt it to a host:

- `--launch`: starts an agent for an assignment. Fields: `{name} {role} {task} {cwd} {brief} {log}`. Without it, `assign` tells the driver where the brief is.
- `--ring`: wakes an agent. Fields: `{address} {text}`.
- `--whoami`: prints the caller's address for `--ring`. It runs on `start`, `init`, and each session start.

Without `--ring`, agents wait with `ledger next --wait`. Examples:

```sh
# Headless Claude Code agents, no wake-ups
ledger init --launch 'claude -p --model haiku --dangerously-skip-permissions "$(cat {brief})" > {log} 2>&1'

# rex-agents panes, woken through Reason
ledger init \
  --launch 'rex-agents launch --tab --name {name} --cwd {cwd} --model claude-opus-5-5 --effort medium --task-file {brief}' \
  --ring 'reason-network send {address} {text}' \
  --whoami 'reason-network who --json | python3 -c "import json,sys; print(next(s[\"address\"] for s in json.load(sys.stdin)[\"sessions\"] if s[\"is_me\"]))"'
```

A ledger started by 0.1 identified agents by Rex pane. After upgrading, the driver takes it over once with `ledger rebind --human --why "<reason>"`.

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
