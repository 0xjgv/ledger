"""ledger: a per-repo state machine for a driver and its long-lived leads."""
import argparse
import shlex
import json
import hashlib
import re
import secrets
import time
import uuid
import sys
from pathlib import Path

from . import model
from .model import ROLES, KINDS, PRIORITIES
from .store import Store, Corrupt
from .world import World

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
VERDICT = re.compile(r"^---\s*\n(?:.*\n)*?verdict:\s*(\S+)\s*\n(?:.*\n)*?---", re.M)


class Refuse(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- context

class Ctx:
    def __init__(self, world):
        self.world = world
        common = world.common_dir()
        if not common:
            raise Refuse("no_repo", "Not inside a git repository.")
        self.store = Store(common / "ledger")
        self.repo = common.parent
        self._alive = {}

    @property
    def config(self):
        try:
            return json.loads((self.store.root / "config.json").read_text())
        except (OSError, ValueError):
            pass
        # Ledgers started by 0.1 ran on Rex and rang Reason; keep them ringing.
        try:
            with open(self.store.log) as fh:
                first = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            first = {}
        return {"doorbell": "reason"} if "block" in first else {}

    def load(self):
        try:
            events, self.torn = self.store.read()
        except Corrupt as exc:
            raise Refuse("corrupt_log", f"{self.store.log}: {exc}") from None
        self.state = model.fold(events)
        return self.state

    def alive(self, session):
        if session not in self._alive:
            self._alive[session] = self.world.alive(session)
        return self._alive[session]

    def health(self, session):
        return model.health(self.alive, session)

    def session(self):
        keys = self.world.sessions()
        need(keys, "no_identity", "No agent identity: run inside a claude or codex session, or set LEDGER_SESSION.")
        return keys[0]

    def address(self):
        """This caller's doorbell address, when the ledger is configured to ring through Reason."""
        return self.world.reason_address() if self.config.get("doorbell") == "reason" else None

    def subject(self, task):
        lead = task["assign"].get("lead")
        return self.world.subject(lead["branch"], task["base"]) if lead else None

    def caller(self, s):
        return s.actor_for(self.world.sessions())


def need(cond, code, message):
    if not cond:
        raise Refuse(code, message)


def task_of(s, slug, open_only=True):
    t = s.tasks.get(slug)
    need(t, "unknown_task", f"Unknown task: {slug}")
    if open_only:
        need(t["status"] == "open", "task_ended", f"Task {slug} is {t['status']}.")
    return t


def as_driver(ctx, s):
    need(s.driver, "no_ledger", "No ledger here. The driver runs `ledger init` first.")
    need(ctx.caller(s) == "driver", "not_driver", "Only the driver may do this.")


def as_bound(ctx, s):
    actor = ctx.caller(s)
    need(actor, "unbound", "This session is not bound. Run `ledger start <task> --token <token>` from your brief.")
    return actor


# --- writes

def mutate(ctx, args, build):
    """Lock, fold, guard via build(state) -> event fields, append, unlock, ring."""
    with ctx.store.lock():
        s = ctx.load()
        rid = getattr(args, "request_id", None)
        fields = build(s)
        if rid:
            fields["request_id"] = rid
            prior = s.request_ids.get(rid)
            if prior:
                same = {k: v for k, v in prior.items() if k in fields} == fields
                need(same, "request_id_conflict", f"Request ID {rid} was used for a different write.")
                return {"ok": True, "event": prior, "repeated": True}
        event = ctx.store.append(s.events, ctx.torn, fields)
        after = model.fold(s.events + [event])
    return {"ok": True, "event": event, "doorbells": ring(ctx, after, event)}


def ring(ctx, s, e):
    """Doorbells after the lock is released. The mail is already in the log."""
    if ctx.config.get("doorbell") != "reason":
        return []
    t = s.tasks.get(e.get("task"))
    k = e["kind"]
    targets = []  # (actor, fyi)
    if k == "start":
        targets = [("driver", True)]
    elif k == "prove" and e["passed"]:
        targets = [("driver", False)]
    elif k in ("accept", "reject"):
        p = s.proofs[e["proof"]]
        targets = [(p["actor"], False)]
        if k == "accept" and t and "reviewer" in t["assign"] and p["role"] == "lead":
            subject = ctx.subject(t)
            if all(model.req_status(s, t, r, subject)[0] in ("accepted", "waived")
                   for r in t["reqs"].values() if r["role"] == "lead"):
                targets.append((s.holder(t, "reviewer"), False))
    elif k in ("ask", "propose", "decide"):
        targets = [(model.recipient(s, s.requests[e["seq"]]), False)]
    elif k in ("answer", "decline"):
        targets = [(model.asker(s, s.requests[e["request"]]), k == "decline")]
    elif k == "decision":
        targets = [("driver", False)]
    elif k == "assign":
        targets = [(e["name"], False)] + [(n, False) for n in e.get("replaced", [])]
    elif k == "require" and t:
        targets = [(s.holder(t, e["req"]["role"]), False)]
    elif k in ("close", "drop", "waive"):
        targets = [(a["name"], True) for a in (t["assign"].values() if t else [])]
    me = (s.driver or {}).get("reason") if e.get("actor") == "driver" else s.directory.get(e.get("actor"))
    out = []
    for actor, fyi in targets:
        if not actor:
            continue
        if actor == "human":
            person = ((s.driver or {}).get("reason") or "").split("/")[0]
            addr, notify = person or None, True
            text = f"[ledger] decision needed #{e['seq']}: {e.get('question')} · run: ledger inbox --human"
        else:
            addr = (s.driver or {}).get("reason") if actor == "driver" else s.directory.get(actor)
            notify = False
            text = f"[ledger] {e.get('task') or '-'} #{e['seq']} {k} from {e.get('actor')} · run: ledger next"
        if not addr or addr == me:
            out.append({"to": actor, "sent": False, "error": "no address" if not addr else "self"})
            continue
        err = ctx.world.ring(addr, text, fyi=fyi, notify=notify)
        out.append({"to": actor, "address": addr, "sent": err is None, **({"error": err} if err else {})})
    return out


def parse_req(text):
    parts = text.split(":", 3)
    need(len(parts) == 4, "bad_requirement", f"Requirement must be id:role:kind:text, got {text!r}")
    rid, role, kind, body = parts
    need(SLUG.match(rid), "bad_requirement", f"Bad requirement id: {rid}")
    need(role in ROLES, "bad_requirement", f"Role must be one of {ROLES}")
    need(kind in KINDS, "bad_requirement", f"Kind must be one of {KINDS}")
    need(body.strip(), "bad_requirement", "Requirement text is empty.")
    return {"id": rid, "role": role, "kind": kind, "text": body.strip()}


# --- commands: driver

def cmd_init(ctx, args):
    session = ctx.session()
    if not ctx.store.exists():
        ctx.store.create()

    def build(s):
        caller = ctx.caller(s)
        if s.driver and caller != "driver":
            need(not caller, "worker", f"{caller} is an assignee and cannot become the driver.")
            state = ctx.health(s.driver["session"])
            need(state == "absent", "driver_alive",
                 f"The driver session is {state}. To take over anyway: ledger rebind --human --why '<reason>'")
        if args.launch is not None or args.doorbell is not None:
            config = ctx.config | {k: v for k, v in (("launch", args.launch), ("doorbell", args.doorbell)) if v is not None}
            (ctx.store.root / "config.json").write_text(json.dumps(config, indent=1) + "\n")
        reason = ctx.address()
        if not s.driver:
            return {"kind": "init", "actor": "driver", "session": session, "reason": reason}
        if caller == "driver":
            need(reason and reason != s.driver["reason"], "noop", "Already the driver.")
            return {"kind": "hello", "actor": "driver", "reason": reason}
        return {"kind": "driver", "actor": "driver", "session": session, "reason": reason, "takeover": True}
    try:
        out = mutate(ctx, args, build)
    except Refuse as exc:
        if exc.code != "noop":
            raise
        out = {"ok": True, "noop": True}
    return out | {"config": ctx.config}


def cmd_rebind(ctx, args):
    need(args.human, "human_only", "rebind needs --human.")
    need(args.why, "missing_why", "rebind needs --why.")
    session = ctx.session()
    reason = ctx.address()
    return mutate(ctx, args, lambda s: (need(s.driver, "no_ledger", "No ledger here."),
                                        {"kind": "driver", "actor": "human", "session": session, "reason": reason,
                                         "why": args.why, "human": True})[1])



def cmd_new(ctx, args):
    need(SLUG.match(args.task), "bad_slug", "Task slug: lowercase letters, digits, hyphens.")
    need(args.priority in PRIORITIES, "bad_priority", f"Priority must be one of {PRIORITIES}")
    reqs = [parse_req(r) for r in args.req]
    need(reqs, "no_requirements", "A task needs at least one --req.")
    need(len({r["id"] for r in reqs}) == len(reqs), "bad_requirement", "Duplicate requirement id.")
    fields = {"kind": "new", "actor": "driver", "task": args.task, "title": args.title, "area": args.area,
              "priority": args.priority, "merge": args.merge, "base": args.base, "reqs": reqs}
    if args.source:
        fields["from"] = args.source

    def build(s):
        as_driver(ctx, s)
        prior = s.tasks.get(args.task)
        if prior:
            first = next(e for e in s.events if e["kind"] == "new" and e["task"] == args.task)
            same = {k: first.get(k) for k in fields} == fields
            need(same, "task_exists", f"Task {args.task} exists with a different payload.")
            raise Refuse("repeat", str(first["seq"]))
        if args.source:
            r = s.requests.get(args.source)
            need(r and r["type"] == "decide" and r["status"] == "done" and not r["settled"],
                 "bad_source", "--from must name a decided, unsettled decide request.")
        return fields
    try:
        return mutate(ctx, args, build)
    except Refuse as exc:
        if exc.code == "repeat":
            return {"ok": True, "repeated": True, "task": args.task}
        raise


def cmd_prioritize(ctx, args):
    need(args.priority in PRIORITIES, "bad_priority", f"Priority must be one of {PRIORITIES}")
    return mutate(ctx, args, lambda s: (as_driver(ctx, s), task_of(s, args.task),
                                        {"kind": "prioritize", "actor": "driver", "task": args.task,
                                         "priority": args.priority})[-1])


def cmd_require(ctx, args):
    req = parse_req(args.req)

    def build(s):
        as_driver(ctx, s)
        t = task_of(s, args.task)
        need(req["id"] not in t["reqs"], "bad_requirement", f"Requirement {req['id']} exists.")
        return {"kind": "require", "actor": "driver", "task": args.task, "req": req}
    return mutate(ctx, args, build)


def cmd_waive(ctx, args):
    def build(s):
        as_driver(ctx, s)
        t = task_of(s, args.task)
        r = t["reqs"].get(args.req)
        need(r and not r["waived"], "unknown_requirement", f"No active requirement {args.req}.")
        status, _ = model.req_status(s, t, r, ctx.subject(t))
        need(status != "accepted", "accepted", "An accepted requirement cannot be waived.")
        return {"kind": "waive", "actor": "driver", "task": args.task, "req": args.req, "why": args.why}
    return mutate(ctx, args, build)


def brief(s, t, role, name, branch, cwd, token):
    mine = [r for r in t["reqs"].values() if r["role"] == role and not r["waived"]]
    lines = [f"# {t['slug']} ({role}): {t['title']}", "", f"You are {name}. Worktree: {cwd} (branch {branch}).",
             "", "Requirements you prove:"]
    lines += [f"- {r['id']} ({r['kind']}): {r['text']}" for r in mine] or ["- none yet"]
    lines += ["", "Rules:",
              f"1. From the worktree, run `ledger start {t['slug']} --token {token}` once.",
              "2. Loop: `ledger next --wait`, do the one action it names, repeat. Stop when it says `done`.",
              f"3. Prove on a clean, committed worktree: `ledger prove {t['slug']} <req> -- <cmd>` or `--file <path>`. "
              "Keep evidence files outside the worktree or committed.",
              f"4. Questions: `ledger ask {t['slug']} \"<text>\" [--blocking]`. Ideas: `ledger propose {t['slug']} \"<idea>\"`.",
              "5. Never ask the user. Launch no other agents."]
    return "\n".join(lines) + "\n"


def cmd_assign(ctx, args):
    need(args.role in ROLES, "bad_role", f"Role must be one of {ROLES}")
    need(SLUG.match(args.name) and args.name not in ("driver", "human"), "bad_name", f"Bad actor name: {args.name}")

    def build(s):
        as_driver(ctx, s)
        t = task_of(s, args.task)
        other = "reviewer" if args.role == "lead" else "lead"
        need(s.holder(t, other) != args.name, "same_actor", "The lead and the reviewer must be different actors.")
        lead = t["assign"].get("lead")
        branch = args.branch or (lead and lead["branch"])
        cwd = str(Path(args.cwd).resolve()) if args.cwd else (lead and lead["cwd"])
        need(branch and cwd, "missing_worktree", "assign needs --branch and --cwd (a reviewer inherits the lead's).")
        prior = t["assign"].get(args.role)
        seq = len(s.events) + 1
        path = ctx.store.briefs / f"{t['slug']}-{args.role}-{seq}.md"
        ctx.store.briefs.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        path.write_text(brief(s, t, args.role, args.name, branch, cwd, token))
        path.chmod(0o600)
        return {"kind": "assign", "actor": "driver", "task": args.task, "role": args.role, "name": args.name,
                "branch": branch, "cwd": cwd, "brief": str(path), "token_sha": _sha(token),
                "launch": "auto" if ctx.config.get("launch") else "manual",
                "replaced": [prior["name"]] if prior and prior["name"] != args.name else []}
    out = mutate(ctx, args, build)
    e = out["event"]
    out["launch"] = launch(ctx, e)
    return out


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def launch_paths(ctx, e):
    d = ctx.store.root / "launch"
    return d / f"{e['seq']}.log", d / f"{e['seq']}.status"


def launch(ctx, e):
    """Run the configured launch template for an assignment, or say how to start the agent by hand."""
    template = ctx.config.get("launch")
    if not template:
        return {"manual": f"Start an agent in {e['cwd']} and give it the brief at {e['brief']}."}
    log, status = launch_paths(ctx, e)
    log.parent.mkdir(parents=True, exist_ok=True)
    fields = {k: e[k] for k in ("name", "role", "task", "cwd", "brief")} | {"log": str(log)}
    command = template.format(**{k: shlex.quote(str(v)) for k, v in fields.items()})
    old = ctx.world.cwd
    ctx.world.cwd = e["cwd"]
    try:
        pid = ctx.world.spawn(command, log, status)
    finally:
        ctx.world.cwd = old
    return {"command": command, "pid": pid, "log": str(log)}


def launch_state(ctx, e):
    """running | exited:<code> | manual, from the launch status file."""
    if e.get("launch") != "auto":
        return "manual"
    _, status = launch_paths(ctx, e)
    try:
        return "exited:" + status.read_text().strip()
    except OSError:
        return "running"


def current_proof(s, seq):
    p = s.proofs.get(seq)
    need(p and p["passed"], "unknown_proof", f"No passing proof #{seq}.")
    t = task_of(s, p["task"])
    r = t["reqs"][p["req"]]
    need(r["proof"] == seq and r["rev"] == p["req_rev"] and not r["waived"], "replaced_proof",
         f"Proof #{seq} is no longer the current proof of {p['req']}.")
    need(p["verdict"] is None, "decided", f"Proof #{seq} was already {p['verdict']}ed.")
    return p, t, r


def cmd_verdict(ctx, args):
    def build(s):
        as_driver(ctx, s)
        p, t, r = current_proof(s, args.proof)
        status, _ = model.req_status(s, t, r, ctx.subject(t))
        need(status == "proved", "stale_proof", f"Proof #{args.proof} is {status}; it no longer matches the candidate.")
        e = {"kind": args.command, "actor": "driver", "task": t["slug"], "proof": args.proof}
        if args.command == "reject":
            need(args.why, "missing_why", "reject needs a reason.")
            e["why"] = args.why
        return e
    return mutate(ctx, args, build)


OPTION = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)(?::\s*(\S.*))?$")


def parse_options(values):
    """[ids], {id: description} from --option "id" or --option "id: description"."""
    ids, notes = [], {}
    for v in values:
        m = OPTION.match(v.strip())
        need(m, "bad_options", f"Option must be 'id' or 'id: description', got {v!r}")
        need(m.group(1) not in ids, "bad_options", f"Duplicate option {m.group(1)}")
        ids.append(m.group(1))
        if m.group(2):
            notes[m.group(1)] = m.group(2)
    return ids, notes


def cmd_decide(ctx, args):
    def build(s):
        as_driver(ctx, s)
        slug = args.task
        if args.source:
            r = s.requests.get(args.source)
            need(r and r["type"] == "propose" and r["status"] == "open", "bad_source", "--from must name an open idea.")
            need(not slug or not r["task"] or slug == r["task"], "bad_source", f"Idea #{args.source} is about {r['task']}.")
            slug = slug or r["task"]
        t = task_of(s, slug) if slug else None
        e = {"kind": "decide", "actor": "driver", "task": slug, "question": args.question,
             "blocking": args.blocking or args.merge, "merge": args.merge, "recommend": args.recommend}
        if args.merge:
            need(t, "missing_task", "A merge decision needs a task.")
            subject = ctx.subject(t)
            need(subject, "no_candidate", "The task branch has no commit.")
            need(model.progress(s, t, subject) == "ready", "not_ready", "A merge decision needs a ready task.")
            e.update(options=["merge", "hold"], candidate=subject["commit"])
        else:
            ids, notes = parse_options(args.option)
            need(len(ids) >= 2, "bad_options", "A decision needs at least two --option values.")
            e["options"] = ids
            if notes:
                e["option_notes"] = notes
        need(not args.recommend or args.recommend in e["options"], "bad_options", "--recommend must be an option.")
        if args.source:
            e["from"] = args.source
        e["attachments"] = [ctx.store.keep(f) for f in args.attach]
        return e
    return mutate(ctx, args, build)


def cmd_settle(ctx, args):
    def build(s):
        as_driver(ctx, s)
        r = s.requests.get(args.request)
        need(r and r["type"] == "decide" and r["status"] == "done" and not r["settled"], "bad_request",
             "Only a decided, unsettled decision can be settled.")
        return {"kind": "settle", "actor": "driver", "request": args.request, "task": r["task"]}
    return mutate(ctx, args, build)


def cmd_decline(ctx, args):
    def build(s):
        as_driver(ctx, s)
        r = s.requests.get(args.request)
        need(r and r["type"] == "propose" and r["status"] == "open", "bad_request", "Only an open idea can be declined.")
        return {"kind": "decline", "actor": "driver", "request": args.request, "task": r["task"], "why": args.why}
    return mutate(ctx, args, build)


def cmd_close(ctx, args):
    def build(s):
        as_driver(ctx, s)
        t = task_of(s, args.task)
        need(t["rev"] == args.rev, "stale_rev", f"Task revision is {t['rev']}, not {args.rev}.")
        subject = ctx.subject(t)
        need(subject and subject["commit"] == args.candidate, "wrong_candidate",
             f"Candidate must be the task branch head {subject and subject['commit']}.")
        need(model.progress(s, t, subject) == "ready", "not_ready", "Every requirement must be accepted or waived.")
        need(not model.blockers(s, t), "blocked", f"Open blocking requests: {model.blockers(s, t)}")
        if t["merge"]:
            merges = [r for r in model.task_requests(s, t) if r["type"] == "decide" and r["merge"]]
            ok = merges and merges[-1]["answer"] and merges[-1]["answer"]["option"] == "merge" \
                and merges[-1]["candidate"] == args.candidate
            need(ok, "no_merge_decision", "This task needs a merge decision approving this candidate.")
        proofs = [r["proof"] for r in t["reqs"].values() if not r["waived"]]
        return {"kind": "close", "actor": "driver", "task": args.task, "candidate": args.candidate,
                "merged": args.merged, "proofs": proofs}
    return mutate(ctx, args, build)


def cmd_drop(ctx, args):
    return mutate(ctx, args, lambda s: (as_driver(ctx, s), task_of(s, args.task),
                                        {"kind": "drop", "actor": "driver", "task": args.task, "why": args.why})[-1])


# --- commands: assignees

def cmd_start(ctx, args):
    session = ctx.session()
    reason = ctx.address()

    def build(s):
        t = task_of(s, args.task)
        role = next((r for r, a in t["assign"].items() if a["token"] == _sha(args.token)), None)
        need(role, "bad_token", f"That token does not match a current assignment on {args.task}.")
        a = t["assign"][role]
        if a["session"] in ctx.world.sessions():
            raise Refuse("noop", "already started")
        if a["session"]:
            state = ctx.health(a["session"])
            need(state == "absent", "bound_elsewhere", f"{a['name']} is bound to another session that is {state}.")
        need(ctx.caller(s) in (None, a["name"]), "other_actor", "This session is already bound to another actor.")
        need(ctx.world.branch(ctx.world.cwd) == a["branch"], "wrong_branch",
             f"Run start from the worktree on branch {a['branch']} ({a['cwd']}).")
        return {"kind": "start", "actor": a["name"], "task": args.task, "role": role, "session": session,
                "reason": reason}
    try:
        return mutate(ctx, args, build)
    except Refuse as exc:
        if exc.code == "noop":
            return {"ok": True, "noop": True}
        raise


def prove_guard(ctx, s, args):
    actor = as_bound(ctx, s)
    t = task_of(s, args.task)
    r = t["reqs"].get(args.req)
    need(r and not r["waived"], "unknown_requirement", f"No active requirement {args.req}.")
    need(s.holder(t, r["role"]) == actor and t["assign"][r["role"]]["session"] in ctx.world.sessions(), "wrong_role",
         f"Only the bound {r['role']} proves {args.req}.")
    need(ctx.world.branch(ctx.world.cwd) == t["assign"][r["role"]]["branch"], "wrong_branch",
         f"Prove from the task branch {t['assign'][r['role']]['branch']}.")
    return actor, t, r


def cmd_prove(ctx, args):
    need(bool(args.cmd) != bool(args.file), "bad_args", "Give either -- <cmd> or --file, not both.")
    s = ctx.load()
    actor, t, r = prove_guard(ctx, s, args)
    cwd = ctx.world.cwd
    need(ctx.world.clean(cwd), "dirty", "Commit or discard changes first; proofs need a clean worktree.")
    before = ctx.world.subject("HEAD", t["base"], cwd=cwd)
    need(before, "no_commit", "HEAD has no commit.")
    evidence, exit_code = [], 0
    if args.cmd:
        cmd = args.cmd
        log = ctx.store.evidence / f"run-{uuid.uuid4().hex}.log"
        ctx.store.evidence.mkdir(parents=True, exist_ok=True)
        exit_code = ctx.world.run(cmd, cwd, log)
        evidence.append(ctx.store.keep(log))
        log.unlink()
    else:
        for f in args.file:
            need(Path(f).is_file() and Path(f).stat().st_size, "missing_file", f"Evidence file missing or empty: {f}")
            evidence.append(ctx.store.keep(f))
            m = VERDICT.search(Path(f).read_text(errors="replace"))
            if m and m.group(1) != "PASS":
                exit_code = 1
    after = ctx.world.subject("HEAD", t["base"], cwd=cwd)
    invalid = None if ctx.world.clean(cwd) and after == before else "worktree changed during the run"

    def build(s2):
        nonlocal invalid
        try:
            prove_guard(ctx, s2, args)
            t2 = s2.tasks[args.task]
            if t2["reqs"][args.req]["rev"] != r["rev"]:
                invalid = invalid or "requirement changed during the run"
        except Refuse as exc:
            invalid = invalid or str(exc)
        return {"kind": "prove", "actor": actor, "task": args.task, "req": args.req, "role": r["role"],
                "req_rev": r["rev"], "subject": before, "cmd": cmd if args.cmd else None, "exit": exit_code,
                "evidence": evidence, "passed": exit_code == 0 and not invalid, "invalid": invalid}
    return mutate(ctx, args, build)


# --- commands: requests

def cmd_ask(ctx, args):
    def build(s):
        actor = as_bound(ctx, s)
        t = task_of(s, args.task)
        from_role = "driver" if actor == "driver" else s.roles_of(actor, t)[0] if s.roles_of(actor, t) else None
        need(from_role, "not_assigned", f"{actor} has no role on {args.task}.")
        to = args.to or ("lead" if actor == "driver" else "driver")
        need(to in ("driver", "lead", "reviewer"), "bad_recipient", "--to must be driver, lead, or reviewer.")
        need(to != from_role, "self", "You cannot ask yourself.")
        return {"kind": "ask", "actor": actor, "task": args.task, "from_role": from_role, "to": to,
                "text": args.text, "blocking": args.blocking}
    return mutate(ctx, args, build)


def cmd_answer(ctx, args):
    def build(s):
        actor = as_bound(ctx, s)
        r = s.requests.get(args.request)
        need(r and r["type"] == "ask", "bad_request", f"No ask #{args.request}.")
        need(r["status"] == "open", "closed_request", f"Ask #{args.request} is {r['status']}.")
        need(model.recipient(s, r) == actor, "not_recipient", f"Ask #{args.request} is not addressed to you.")
        return {"kind": "answer", "actor": actor, "task": r["task"], "request": args.request, "text": args.text}
    return mutate(ctx, args, build)


def cmd_cancel(ctx, args):
    def build(s):
        actor = as_bound(ctx, s)
        r = s.requests.get(args.request)
        need(r and r["type"] in ("ask", "decide") and r["status"] == "open", "bad_request",
             f"No open ask or decision #{args.request}.")
        need(actor == "driver" or model.asker(s, r) == actor, "not_asker", "Only the asker or the driver may cancel.")
        return {"kind": "cancel", "actor": actor, "task": r["task"], "request": args.request}
    return mutate(ctx, args, build)


def cmd_propose(ctx, args):
    def build(s):
        actor = as_bound(ctx, s)
        need(actor != "driver", "driver", "The driver decides directly; proposals come from leads.")
        if args.task:
            t = task_of(s, args.task)
            need(s.roles_of(actor, t), "not_assigned", f"{actor} has no role on {args.task}.")
        return {"kind": "propose", "actor": actor, "task": args.task, "area": args.area, "text": args.text,
                "attachments": [ctx.store.keep(f) for f in args.attach]}
    return mutate(ctx, args, build)


def cmd_decision(ctx, args):
    need(args.human, "human_only", "A decision needs --human.")
    need(args.why, "missing_why", "A decision needs --why.")

    def build(s):
        r = s.requests.get(args.request)
        need(r and r["type"] == "decide", "bad_request", f"No decision request #{args.request}.")
        if r["status"] == "done":
            need(r["answer"]["option"] == args.option, "conflict", f"#{args.request} was decided {r['answer']['option']}.")
            raise Refuse("repeat", "same answer")
        need(r["status"] == "open", "closed_request", f"#{args.request} is {r['status']}.")
        need(args.option in r["options"], "bad_option", f"Option must be one of {r['options']}")
        return {"kind": "decision", "actor": "human", "task": r["task"], "request": args.request,
                "option": args.option, "why": args.why}
    try:
        return mutate(ctx, args, build)
    except Refuse as exc:
        if exc.code == "repeat":
            return {"ok": True, "repeated": True}
        raise


def cmd_hello(ctx, args):
    reason = ctx.address()

    def build(s):
        actor = as_bound(ctx, s)
        known = s.driver["reason"] if actor == "driver" else s.directory.get(actor)
        if not reason or reason == known:
            raise Refuse("noop", "")
        return {"kind": "hello", "actor": actor, "reason": reason}
    try:
        return mutate(ctx, args, build)
    except Refuse as exc:
        if exc.code == "noop":
            return {"ok": True, "noop": True}
        raise


# --- reads

def views(ctx, s):
    out = {}
    for t in s.open_tasks():
        subject = ctx.subject(t)
        reqs = {}
        for r in t["reqs"].values():
            status, proof = model.req_status(s, t, r, subject)
            reqs[r["id"]] = {"role": r["role"], "kind": r["kind"], "status": status, "proof": proof, "text": r["text"]}
        out[t["slug"]] = {
            "task": t["slug"], "title": t["title"], "area": t["area"], "priority": t["priority"], "rev": t["rev"],
            "progress": model.progress(s, t, subject), "candidate": subject and subject["commit"],
            "assignees": {role: {"name": a["name"], "branch": a["branch"], "cwd": a["cwd"],
                                 "health": ctx.health(a["session"]) if a["session"] else "not-started"}
                          for role, a in t["assign"].items()},
            "blockers": model.blockers(s, t), "requirements": reqs}
    return out


def order(v):
    return (v["priority"], v["rev"], v["task"])


def driver_next(ctx, s, vs):
    tasks = sorted(vs.values(), key=order)
    box = model.inbox(s, "driver")
    reviews = [r for r in box if r["type"] == "review" and vs.get(r["task"], {}).get("requirements", {})
               .get(s.proofs[r["proof"]]["req"], {}).get("status") == "proved"]
    decided = [r for r in s.requests.values() if r["type"] == "decide" and r["status"] == "done" and not r["settled"]]
    asks = [r for r in box if r["type"] == "ask"]
    ideas = [r for r in box if r["type"] == "propose"]
    for r in asks:
        if r["blocking"]:
            return item("answer", r, f"{r['from']} asks: {r['text']}", [f"ledger answer {r['seq']} \"<answer>\""])
    for r in decided:
        opt = r["answer"]["option"]
        if r["merge"] and opt == "merge":
            v = vs.get(r["task"])
            return item("merge", r, f"You approved merging {r['task']} at {r['candidate']}. Merge the PR, then close.",
                        [f"ledger close {r['task']} --candidate {r['candidate']} --rev {v and v['rev']} --merged <sha>"])
        return item("act", r, f"Decision #{r['seq']} ({r['text']}): {opt}. Because: {r['answer']['why']}. Act on it.",
                    [f"ledger new <slug> \"<title>\" --from {r['seq']} --req …", f"ledger settle {r['seq']}"])
    for r in reviews:
        p = s.proofs[r["proof"]]
        return item("validate", r, f"{p['actor']} proved {p['req']} on {p['task']} (exit {p['exit']}) at "
                    f"{p['subject']['commit'][:12]}. Read the evidence, then accept or reject.",
                    [f"ledger accept {p['seq']}", f"ledger reject {p['seq']} \"<reason>\""],
                    evidence=[e["path"] for e in p["evidence"]])
    for v in tasks:
        for role, a in v["assignees"].items():
            e = _assign_event(s, v["task"], role)
            state = launch_state(ctx, e)
            gone = a["health"] == "absent" or (a["health"] == "not-started" and state.startswith("exited:")
                                                and state != "exited:0")
            if gone:
                return item("relaunch", v, f"{role} {a['name']} of {v['task']} is gone ({a['health']}, launch {state}). "
                            "Assign it again; that issues a fresh brief and token.",
                            [f"ledger assign {v['task']} {a['name']} --role {role}"])
    for v in tasks:
        if v["progress"] == "unassigned":
            return item("assign", v, f"Assign a lead to {v['task']}.",
                        [f"ledger assign {v['task']} <name> --branch <branch> --cwd <worktree>"])
        if "reviewer" not in v["assignees"] and any(r["role"] == "reviewer" and r["status"] != "waived"
                                                    for r in v["requirements"].values()):
            return item("assign", v, f"Assign a reviewer to {v['task']}.",
                        [f"ledger assign {v['task']} <name> --role reviewer"])
    for v in tasks:
        if v["progress"] == "ready" and not v["blockers"]:
            t = s.tasks[v["task"]]
            if t["merge"]:
                return item("decide-merge", v, f"{v['task']} is ready at {v['candidate']}. Ask the human to merge.",
                            [f"ledger decide {v['task']} \"Merge {v['task']}?\" --merge --recommend merge"])
            return item("close", v, f"{v['task']} is ready.",
                        [f"ledger close {v['task']} --candidate {v['candidate']} --rev {v['rev']}"])
    for r in ideas:
        return item("idea", r, f"{r['from']} proposes: {r['text']}",
                    [f"ledger decide {r['task'] or ''} \"<question>\" --option … --from {r['seq']}".replace("  ", " "),
                     f"ledger decline {r['seq']} --why \"<reason>\""])
    for r in asks:
        return item("answer", r, f"{r['from']} asks: {r['text']}", [f"ledger answer {r['seq']} \"<answer>\""])
    if s.tasks and not tasks and not box and not decided:
        return {"action": "done", "prompt": "Every task has ended and nothing waits on you."}
    return {"action": "idle", "prompt": "Nothing waits on you. End your turn, or run `ledger next --wait`."}


def _assign_event(s, slug, role):
    seq = s.tasks[slug]["assign"][role]["seq"]
    return s.events[seq - 1]


def lead_next(ctx, s, vs, name, keys):
    mine = []
    for t in s.open_tasks():
        for role, a in t["assign"].items():
            if a["name"] == name:
                mine.append((t, role, a))
    for e in s.events:
        t = s.tasks.get(e.get("task"))
        if e["kind"] == "start" and e["actor"] == name and t["status"] == "open" and s.holder(t, e["role"]) != name:
            return {"action": "stop", "task": t["slug"], "prompt": f"{t['slug']} was reassigned. Stop work on it."}
    for r in sorted(model.inbox(s, name), key=lambda r: r["seq"]):
        return item("answer", r, f"{model.asker(s, r)} asks: {r['text']}", [f"ledger answer {r['seq']} \"<answer>\""])
    mine.sort(key=lambda x: order(vs[x[0]["slug"]]))
    for t, role, a in mine:
        if a["session"] not in keys:
            return {"action": "start", "task": t["slug"], "cwd": a["cwd"], "brief": a["brief"],
                    "prompt": f"You hold a new assignment. Read {a['brief']}, then run its start command from {a['cwd']}.",
                    "commands": [f"cat {a['brief']}"]}
    if not mine:
        return {"action": "done", "prompt": "You hold no open assignment. Stop."}
    for t, role, a in mine:
        waiting = [r for r in model.task_requests(s, t) if r["type"] == "ask" and r["status"] == "open"
                   and r["blocking"] and model.asker(s, r) == name]
        if waiting:
            continue
        if role == "reviewer" and not lead_done(vs[t["slug"]]):
            continue
        for rid, rv in vs[t["slug"]]["requirements"].items():
            if rv["role"] == role and rv["status"] in ("missing", "rejected", "stale"):
                why = ""
                if rv["status"] == "rejected":
                    why = f" Rejected: {s.proofs[rv['proof']].get('why')}."
                return {"action": "prove", "task": t["slug"], "cwd": a["cwd"],
                        "prompt": f"{rv['status'].capitalize()} {rid}: {rv['text']}.{why} Do the work, commit, prove.",
                        "commands": [f"ledger prove {t['slug']} {rid} -- <cmd>", f"ledger prove {t['slug']} {rid} --file <path>"]}
    return {"action": "idle", "prompt": "Everything waits on others. Run `ledger next --wait`."}


def lead_done(view):
    """A reviewer's work starts once every lead requirement is accepted or waived."""
    return all(r["status"] in ("accepted", "waived") for r in view["requirements"].values() if r["role"] == "lead")


def item(action, x, prompt, commands, **extra):
    return {"action": action, "task": x.get("task"), "request": x.get("seq") if "type" in x else None,
            "prompt": prompt, "commands": commands, **extra}


def next_once(ctx):
    s = ctx.load()
    ctx._alive = {}
    vs = views(ctx, s)
    actor = as_bound(ctx, s)
    if actor == "driver":
        return {"actor": "driver", **driver_next(ctx, s, vs)}
    return {"actor": actor, **lead_next(ctx, s, vs, actor, ctx.world.sessions())}


def cmd_next(ctx, args):
    """One action. With --wait, block until something other than idle, polling the log."""
    out = next_once(ctx)
    deadline = time.monotonic() + (args.timeout if args and args.wait else 0)
    stamp = None
    while out["action"] == "idle" and time.monotonic() < deadline:
        time.sleep(2)
        now = ctx.store.log.stat().st_mtime_ns
        if now != stamp:
            stamp = now
            out = next_once(ctx)
        elif int(time.monotonic()) % 30 == 0:
            out = next_once(ctx)  # liveness and launch state change without a log write
    return out


def cmd_inbox(ctx, args):
    s = ctx.load()
    actor = "human" if args.human else ctx.caller(s)
    need(actor, "unbound", "Unknown caller; use --human for the human queue.")
    box = model.inbox(s, actor)
    key = (lambda r: (not r["blocking"], s.tasks[r["task"]]["priority"] if r["task"] in s.tasks else "p9", r["seq"]))
    return {"actor": actor, "requests": sorted(box, key=key)}


def cmd_board(ctx, args):
    s = ctx.load()
    vs = views(ctx, s)
    rows = [{k: v[k] for k in ("task", "title", "area", "priority", "progress", "blockers")} |
            {"assignees": {r: f"{a['name']} ({a['health']})" for r, a in v["assignees"].items()},
             "waiting_on": sorted({f"{rv['role']}:{rid}" for rid, rv in v["requirements"].items()
                                   if rv["status"] in ("missing", "rejected", "stale")})}
            for v in sorted(vs.values(), key=order)]
    return {"tasks": rows, "pending_requests": len([r for r in s.requests.values() if r["status"] == "open"])}


def cmd_show(ctx, args):
    s = ctx.load()
    t = task_of(s, args.task, open_only=False)
    if t["status"] != "open":
        return {"task": t["slug"], "status": t["status"], "closed": t["closed"]}
    return views(ctx, s)[t["slug"]] | {"requests": model.task_requests(s, t)}


def cmd_log(ctx, args):
    s = ctx.load()
    return {"events": [e for e in s.events if e["seq"] > args.since]}


# --- hook

def hook_session_start(world):
    """SessionStart hook: refresh the Reason address and inject the next action. Never fails."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        world.cwd = payload.get("cwd") or world.cwd
        ctx = Ctx(world)
        if not ctx.store.exists():
            return 0
        s = ctx.load()
        if not ctx.caller(s):
            return 0
        cmd_hello(ctx, argparse.Namespace(request_id=None))
        nxt = cmd_next(ctx, None)
        text = (f"Ledger ({ctx.repo}): you are {nxt['actor']}. Work by looping on `ledger next --wait` until it says done.\n"
                f"Next: {json.dumps(nxt)}")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:  # a hook must never break a session
        pass
    return 0


# --- argv

def parser():
    p = argparse.ArgumentParser(prog="ledger", description=__doc__)
    p.add_argument("--request-id", help="retry key: the same ID and payload returns the earlier result")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, *args):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        for a in args:
            sp.add_argument(*a[0], **a[1])
        return sp
    A = lambda *names, **kw: (names, kw)
    task, why = A("task"), A("--why", required=False)
    add("init", cmd_init, A("--launch", help="launch template: {name} {role} {task} {cwd} {brief} {log}"),
        A("--doorbell", choices=("none", "reason")))
    add("rebind", cmd_rebind, A("--human", action="store_true"), why)
    add("new", cmd_new, task, A("title"), A("--req", action="append", default=[]), A("--area"),
        A("--priority", default="p2"), A("--merge", action="store_true"), A("--base", default="main"),
        A("--from", dest="source", type=int))
    add("prioritize", cmd_prioritize, task, A("priority"))
    add("require", cmd_require, task, A("req"))
    add("waive", cmd_waive, task, A("req"), A("why"))
    add("assign", cmd_assign, task, A("name"), A("--role", default="lead"), A("--branch"), A("--cwd"))
    add("accept", cmd_verdict, A("proof", type=int))
    add("reject", cmd_verdict, A("proof", type=int), A("why"))
    add("decide", cmd_decide, A("task", nargs="?"), A("question"), A("--option", action="append", default=[]),
        A("--recommend"), A("--blocking", action="store_true"), A("--merge", action="store_true"),
        A("--attach", action="append", default=[]), A("--from", dest="source", type=int))
    add("settle", cmd_settle, A("request", type=int))
    add("decline", cmd_decline, A("request", type=int), A("--why", required=True))
    add("close", cmd_close, task, A("--candidate", required=True), A("--rev", type=int, required=True), A("--merged"))
    add("drop", cmd_drop, task, A("why"))
    add("start", cmd_start, task, A("--token", required=True))
    add("prove", cmd_prove, task, A("req"), A("--file", action="append", default=[]))
    add("ask", cmd_ask, task, A("text"), A("--to"), A("--blocking", action="store_true"))
    add("answer", cmd_answer, A("request", type=int), A("text"))
    add("cancel", cmd_cancel, A("request", type=int))
    add("propose", cmd_propose, A("task", nargs="?"), A("text"), A("--area"), A("--attach", action="append", default=[]))
    add("decision", cmd_decision, A("request", type=int), A("option"), A("--human", action="store_true"), why)
    add("hello", cmd_hello)
    add("next", cmd_next, A("--wait", action="store_true"), A("--timeout", type=int, default=100, help="seconds to wait (default 100)"))
    add("inbox", cmd_inbox, A("--human", action="store_true"))
    add("board", cmd_board)
    add("show", cmd_show, task)
    add("log", cmd_log, A("--since", type=int, default=0))
    return p


def main(argv=None, world=None):
    argv = sys.argv[1:] if argv is None else argv
    world = world or World()
    if argv[:2] == ["_hook", "session-start"]:
        return hook_session_start(world)
    if argv == ["version"]:
        from . import __version__
        print(__version__)
        return 0
    if argv[:1] == ["update"]:
        from .update import update
        need_version = argv[1:]
        if need_version and (len(need_version) != 1 or not re.fullmatch(r"v?\d+\.\d+\.\d+", need_version[0])):
            print("usage: ledger update [vX.Y.Z]", file=sys.stderr)
            return 2
        return update(need_version[0] if need_version else None)
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = parser().parse_args(argv)
    args.cmd = cmd
    try:
        ctx = Ctx(world)
        if args.fn is not cmd_init:
            need(ctx.store.exists(), "no_ledger", "No ledger here. The driver runs `ledger init` first.")
        out = args.fn(ctx, args)
        print(json.dumps(out, indent=1, default=str))
        return 0
    except Refuse as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, indent=1))
        return 2
