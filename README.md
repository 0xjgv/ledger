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

Install: symlink `bin/ledger` onto PATH. Tests: `python3.13 -m unittest discover -s tests`.
