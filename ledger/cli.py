"""ledger: tasks, leads, states, messages, and an inbox, kept per repo for a driver agent and its leads."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, model
from .store import Store, Corrupt

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class Refuse(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def need(cond, code, message):
    if not cond:
        raise Refuse(code, message)


def open_store(cwd=None):
    if d := os.environ.get("LEDGER_DIR"):
        need(os.path.isabs(d) and Path(d).parent.is_dir(), "bad_dir",
             f"LEDGER_DIR must be an absolute path whose parent exists: {d}")
        return Store(d)
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       cwd=cwd, capture_output=True, text=True)
    need(r.returncode == 0, "no_repo", "Not inside a git repository.")
    return Store(Path(r.stdout.strip()) / "ledger")


def load(store):
    try:
        events, torn = store.read()
    except Corrupt as exc:
        raise Refuse("corrupt_log", f"{store.log}: {exc}") from None
    return model.fold(events), events, torn


def write(store, actor, build):
    """Lock, fold, check via build(state) -> event fields, append."""
    with store.lock():
        s, events, torn = load(store)
        return store.append(events, torn, {"v": 3, "actor": actor, **build(s)})


def task_of(s, tid):
    need(tid in s.tasks, "unknown_task", f"Unknown task: {tid}")
    return s.tasks[tid]


def item_of(s, n):
    need(n in s.items, "unknown_item", f"Unknown inbox item: #{n}")
    return s.items[n]


def wait_for(store, done, timeout):
    """Poll until done(state) is truthy or timeout seconds pass. Returns the last state."""
    end = time.monotonic() + timeout
    while True:
        s = load(store)[0]
        if done(s) or time.monotonic() >= end:
            return s
        time.sleep(2)


def mark_seen(store, actor, items):
    """Record that actor has read the answers to its own asks among items."""
    ns = [i["n"] for i in items if i["status"] == "answered" and i["from"] == actor and not i["seen"]]
    if ns:
        write(store, actor, lambda s: {"kind": "seen", "items": ns})


# --- commands: each returns (data, text)

def cmd_new(store, a):
    need(NAME.match(a.task), "bad_id", "Task IDs are lowercase letters, digits, - and _.")
    if a.lead:
        need(NAME.match(a.lead), "bad_name", f"Bad lead name: {a.lead}")

    def build(s):
        need(a.task not in s.tasks, "task_exists", f"Task {a.task} exists.")
        return {"kind": "new", "task": a.task, "title": a.title, "lead": a.lead}
    e = write(store, a.actor, build)
    return e, f"new {a.task}: {a.title}" + (f" (lead {a.lead})" if a.lead else "")


def cmd_set(store, a):
    fields = {}
    for pair in a.pairs:
        key, eq, value = pair.partition("=")
        need(eq and NAME.match(key), "bad_field", f"Expected key=value, got: {pair}")
        fields[key] = value
    if fields.get("lead"):
        need(NAME.match(fields["lead"]), "bad_name", f"Bad lead name: {fields['lead']}")

    def build(s):
        t = task_of(s, a.task)
        if "state" in fields:
            new, old = fields["state"], t["state"]
            need(new in model.STATES, "bad_state", f"States: {', '.join(model.STATES)}.")
            need(new == old or new in model.FLOW[old], "bad_transition",
                 f"{a.task} is {old}; it can move to: {', '.join(sorted(model.FLOW[old]))}.")
        return {"kind": "set", "task": a.task, "fields": fields}
    e = write(store, a.actor, build)
    return e, f"{a.task}: " + " ".join(f"{k}={v}" for k, v in fields.items())


def cmd_say(store, a):
    e = write(store, a.actor, lambda s: task_of(s, a.task) and {"kind": "say", "task": a.task, "text": a.text})
    return e, f"#{e['seq']} on {a.task}"


def cmd_ask(store, a):
    need(NAME.match(a.to), "bad_name", f"Bad recipient: {a.to}")
    task = None if a.task == "-" else a.task

    def build(s):
        if task:
            t = task_of(s, task)
            need(t["state"] not in model.ENDED, "task_ended", f"Task {task} is {t['state']}.")
        return {"kind": "ask", "task": task, "to": a.to, "text": a.text, "options": a.option}
    e = write(store, a.actor, build)
    if not a.wait:
        return e, f"#{e['seq']} asked {a.to}"
    item = wait_for(store, lambda s: s.items[e["seq"]]["status"] != "open", a.timeout).items[e["seq"]]
    mark_seen(store, a.actor, [item])
    return item, render_item(item)


def cmd_answer(store, a):
    def build(s):
        item = item_of(s, a.item)
        need(item["status"] == "open", "item_closed", f"#{a.item} is {item['status']}: {item['answer']}")
        return {"kind": "answer", "item": a.item, "task": item["task"], "text": a.text}
    write(store, a.actor, build)
    return {"item": a.item}, f"#{a.item} answered"


def cmd_inbox(store, a):
    name = None if a.all else (a.name or a.actor)
    s = wait_for(store, lambda s: any(i["n"] > a.after or i["status"] != "open" for i in s.inbox(name)),
                 a.timeout if a.wait else 0)
    items = s.inbox(name)
    if name == a.actor:
        mark_seen(store, a.actor, items)
    text = "\n".join(render_item(i) for i in items) or f"inbox empty{'' if name is None else ' for ' + name}"
    return items, f"{text}\nhead: {s.head}"


def cmd_board(store, a):
    s = load(store)[0]
    tasks = [t for t in s.tasks.values() if a.all or t["state"] not in model.ENDED]
    rows = [{**t, "asks": sum(1 for i in s.inbox() if i["task"] == t["id"])} for t in tasks]
    counts = {}
    for i in s.inbox():
        counts[i["to"]] = counts.get(i["to"], 0) + 1
    lines = [f"{'TASK':<24} {'STATE':<8} {'LEAD':<16} {'ASKS':<4} {'UPDATED':<20} TITLE"]
    lines += [f"{r['id']:<24} {r['state']:<8} {r['lead'] or '-':<16} {r['asks']:<4} {r['updated']:<20} {r['title']}"
              for r in rows]
    lines.append("inbox: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
    return {"tasks": rows, "inbox": counts, "head": s.head}, "\n".join(lines)


def cmd_show(store, a):
    s = load(store)[0]
    if a.ref.lstrip("#").isdigit():
        item = item_of(s, int(a.ref.lstrip("#")))
        return item, render_item(item)
    t = task_of(s, a.ref)
    history = s.history.get(t["id"], [])
    head = [f"{t['id']}: {t['title']}", f"state {t['state']} · lead {t['lead'] or '-'} · created {t['created']}"]
    head += [f"{k}: {v}" for k, v in t["fields"].items()]
    return {**t, "history": history}, "\n".join(head + [""] + [render_event(e) for e in history])


def cmd_version(store, a):
    return {"version": __version__}, __version__


# --- rendering

def render_item(i):
    task = f" [{i['task']}]" if i["task"] else ""
    lines = [f"#{i['n']}{task} {i['from']} → {i['to']} ({i['at']}): {i['text']}"]
    lines += [f"    option {o}" for o in i["options"]]
    if i["status"] != "open":
        lines.append(f"    {i['status']} by {i['by']}: {i['answer']}")
    return "\n".join(lines)


def render_event(e):
    k = e["kind"]
    body = {"new": lambda: f"created: {e.get('title')}",
            "set": lambda: "set " + " ".join(f"{k2}={v}" for k2, v in e.get("fields", {}).items()),
            "say": lambda: e.get("text"),
            "ask": lambda: f"ask #{e['seq']} → {e.get('to')}: {e.get('text')}",
            "answer": lambda: f"answer #{e.get('item')}: {e.get('text')}"}.get(k)
    text = body() if body and e.get("v") == 3 else f"{k} " + (e.get("text") or e.get("question") or e.get("why") or e.get("name") or "")
    return f"#{e['seq']} {e['at']} {e.get('actor') or '-'}: {text}".rstrip()


# --- hook

def hook_session_start():
    """SessionStart hook: remind an agent named by LEDGER_AS of its tasks and inbox. Never fails."""
    try:
        name = os.environ.get("LEDGER_AS")
        if not name:
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        store = open_store(payload.get("cwd"))
        if not store.log.exists():
            return 0
        s = load(store)[0]
        mine = [f"{t['id']} ({t['state']})" for t in s.tasks.values()
                if t["lead"] == name and t["state"] not in model.ENDED]
        text = (f"Ledger: you are {name}. Your tasks: {', '.join(mine) or 'none'}. "
                f"Open inbox items for you: {len(s.inbox(name))} (`ledger inbox`).")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:  # a hook must never break a session
        pass
    return 0


# --- argv

def parser():
    p = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="actor", help="your name (default: $LEDGER_AS, else driver)")
    common.add_argument("--json", action="store_true", help="print JSON")

    def add(name, fn, help, *args):
        sp = sub.add_parser(name, parents=[common], help=help)
        sp.set_defaults(fn=fn)
        for names, kw in args:
            sp.add_argument(*names, **kw)

    def A(*names, **kw):
        return names, kw
    wait = (A("--wait", action="store_true"), A("--timeout", type=int, default=100, help="seconds (default 100)"))
    add("new", cmd_new, "add a task", A("task"), A("title"), A("--lead"))
    add("set", cmd_set, "set state, lead, title, or any field (key= clears)", A("task"), A("pairs", nargs="+"))
    add("say", cmd_say, "post a message on a task", A("task"), A("text"))
    add("ask", cmd_ask, "open an inbox item on a task (or - for none)", A("task"), A("text"),
        A("--to", default="driver"), A("--option", action="append", default=[], help='"id: description"'), *wait)
    add("answer", cmd_answer, "answer and close an inbox item", A("item", type=lambda v: int(v.lstrip("#"))), A("text"))
    add("inbox", cmd_inbox, "open items for you, and new answers to your asks", A("--for", dest="name"),
        A("--all", action="store_true"), *wait,
        A("--after", type=int, default=0, help="with --wait: wake only for items numbered above SEQ, or answers"))
    add("board", cmd_board, "tasks and inbox counts", A("--all", action="store_true", help="include done and dropped"))
    add("show", cmd_show, "a task's history, or one item (#n)", A("ref"))
    add("version", cmd_version, "print the version")
    sub.add_parser("update", help="install the latest or a given release").add_argument("version", nargs="?")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:2] == ["_hook", "session-start"]:
        return hook_session_start()
    args = parser().parse_args(argv)
    if args.command == "update":
        if args.version and not re.fullmatch(r"v?\d+\.\d+\.\d+", args.version):
            print("usage: ledger update [vX.Y.Z]", file=sys.stderr)
            return 2
        from .update import update
        return update(args.version)
    args.actor = args.actor or os.environ.get("LEDGER_AS") or "driver"
    try:
        need(NAME.match(args.actor), "bad_name", f"Bad name: {args.actor}")
        data, text = args.fn(None if args.command == "version" else open_store(), args)
    except Refuse as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}) if args.json else f"ledger: {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2) if args.json else text)
    return 0
