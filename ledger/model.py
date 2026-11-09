"""Fold the event log into tasks and inbox items. Reads 0.2 logs too."""

STATES = ("todo", "active", "blocked", "review", "done", "dropped")
FLOW = {
    "todo": {"active", "dropped"},
    "active": {"blocked", "review", "done", "dropped"},
    "blocked": {"active", "dropped"},
    "review": {"active", "done", "dropped"},
    "done": {"active"},
    "dropped": {"todo"},
}
ENDED = ("done", "dropped")


class State:
    def __init__(self):
        self.tasks = {}    # id -> task
        self.items = {}    # seq -> inbox item
        self.history = {}  # task id -> events

    def inbox(self, name=None):
        """Open items for name (all open items when None), plus answers to name's asks not yet seen."""
        return [i for i in self.items.values() if (i["status"] == "open" and name in (None, i["to"]))
                or (name and i["status"] == "answered" and i["from"] == name and not i["seen"])]


def fold(events):
    s = State()
    for e in events:
        if e.get("v") == 3:
            _apply(s, e)
        else:
            _apply_v2(s, e)
        if e.get("task") in s.tasks:
            s.tasks[e["task"]]["updated"] = e["at"]
            s.history.setdefault(e["task"], []).append(e)
    return s


def _apply(s, e):
    k = e["kind"]
    if k == "new":
        _new(s, e, e.get("lead"))
    elif k == "set":
        _set(s, e, e["fields"])
    elif k == "ask":
        _ask(s, e, e["to"], e["text"], e.get("options", []))
    elif k == "answer":
        _answer(s, e, e["item"], e["text"])
    elif k == "seen":
        for n in e["items"]:
            s.items[n]["seen"] = True


def _new(s, e, lead):
    s.tasks[e["task"]] = {"id": e["task"], "title": e["title"], "state": "todo", "lead": lead,
                          "fields": {}, "created": e["at"], "updated": e["at"]}


def _set(s, e, fields):
    t = s.tasks[e["task"]]
    for key, value in fields.items():
        if key in ("state", "lead", "title"):
            t[key] = value or None
        elif value:
            t["fields"][key] = value
        else:
            t["fields"].pop(key, None)
    if t["state"] in ENDED:
        for i in s.items.values():
            if i["task"] == t["id"] and i["status"] == "open":
                i.update(status="closed", answer=f"task {t['state']}", by=e.get("actor"), answered=e["at"])


def _ask(s, e, to, text, options):
    s.items[e["seq"]] = {"n": e["seq"], "task": e.get("task"), "from": e.get("actor"), "to": to, "text": text,
                         "options": options, "at": e["at"], "status": "open", "answer": None, "by": None,
                         "seen": False}


def _answer(s, e, n, text, seen=False):
    item = s.items.get(n)
    if item and item["status"] == "open":
        item.update(status="answered", answer=text, by=e.get("actor"), answered=e["at"],
                    seen=seen or e.get("actor") == item["from"])


def _apply_v2(s, e):
    """0.1/0.2 events: keep tasks, leads, lifecycle, and requests; drop proofs and identity."""
    k, t = e["kind"], s.tasks.get(e.get("task"))
    if k == "new":
        _new(s, e, None)
    elif k == "assign":
        _set(s, e, {"lead" if e["role"] == "lead" else e["role"]: e["name"]})
    elif k == "start" and t and t["state"] == "todo" and e.get("role") == "lead":
        _set(s, e, {"state": "active"})
    elif k in ("close", "drop"):
        _set(s, e, {"state": "done" if k == "close" else "dropped"})
    elif k in ("ask", "propose", "decide"):
        to = e.get("to") or {"propose": "driver", "decide": "human"}[k]
        if t and to == "lead":
            to = t["lead"]
        elif t and to in t["fields"] and to not in ("driver", "human"):
            to = t["fields"][to]
        notes = e.get("option_notes") or {}
        options = [f"{o}: {notes[o]}" if notes.get(o) else o for o in e.get("options") or []]
        _ask(s, e, to, e.get("text") or e.get("question"), options)
        if k == "decide" and e.get("from"):
            _answer(s, e, e["from"], f"forwarded as #{e['seq']}", seen=True)
    elif k == "answer":
        _answer(s, e, e["request"], e["text"], seen=True)
    elif k == "decision":
        _answer(s, e, e["request"], f"{e['option']}: {e.get('why') or ''}".rstrip(": "), seen=True)
    elif k in ("cancel", "decline"):
        word = "cancelled" if k == "cancel" else "declined"
        _answer(s, e, e["request"], word + (f": {e['why']}" if e.get("why") else ""), seen=True)
