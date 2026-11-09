"""Pure fold of the event log, plus views computed from state and observations."""

ROLES = ("lead", "reviewer")
KINDS = ("tree", "commit", "diff")
PRIORITIES = ("p0", "p1", "p2", "p3")


class State:
    def __init__(self):
        self.driver = None          # {"block", "reason"}
        self.directory = {}         # actor -> Reason address
        self.tasks = {}             # slug -> task dict
        self.proofs = {}            # seq -> proof event
        self.requests = {}          # seq -> request dict
        self.request_ids = {}       # request_id -> event
        self.events = []

    # --- queries
    def actor_for_block(self, block):
        if not block:
            return None
        if self.driver and self.driver["block"] == block:
            return "driver"
        for t in self.open_tasks():
            for a in t["assign"].values():
                if a["block"] == block:
                    return a["name"]
        return None

    def open_tasks(self):
        return [t for t in self.tasks.values() if t["status"] == "open"]

    def roles_of(self, name, task):
        return [r for r, a in task["assign"].items() if a["name"] == name]

    def holder(self, task, role):
        a = task["assign"].get(role)
        return a["name"] if a else None


def fold(events):
    s = State()
    for e in events:
        s.events.append(e)
        if e.get("request_id"):
            s.request_ids[e["request_id"]] = e
        _apply(s, e)
    return s


def _apply(s, e):
    k = e["kind"]
    actor = e.get("actor")
    if e.get("reason") and actor and actor not in ("driver", "human"):
        s.directory[actor] = e["reason"]
    t = s.tasks.get(e.get("task"))
    if t is not None:
        t["rev"] = e["seq"]
    if k in ("init", "driver"):
        s.driver = {"block": e["block"], "reason": e.get("reason")}
    elif k == "hello" and actor == "driver":
        s.driver["reason"] = e["reason"]
    elif k == "new":
        s.tasks[e["task"]] = {
            "slug": e["task"], "title": e["title"], "area": e.get("area"), "priority": e.get("priority", "p2"),
            "merge": e.get("merge", False), "base": e.get("base", "main"), "status": "open", "rev": e["seq"],
            "seq": e["seq"], "reqs": {}, "assign": {}, "closed": None}
        for r in e["reqs"]:
            _add_req(s.tasks[e["task"]], r, e["seq"])
        if e.get("from"):
            s.requests[e["from"]]["settled"] = True
    elif k == "prioritize":
        t["priority"] = e["priority"]
    elif k == "require":
        _add_req(t, e["req"], e["seq"])
    elif k == "waive":
        t["reqs"][e["req"]]["waived"] = e["why"]
        _drop_review_requests(s, t, e["req"])
    elif k == "assign":
        t["assign"][e["role"]] = {"name": e["name"], "branch": e["branch"], "cwd": e["cwd"], "seq": e["seq"],
                                  "block": None, "brief": e.get("brief")}
    elif k == "start":
        t["assign"][e["role"]]["block"] = e["block"]
    elif k == "prove":
        s.proofs[e["seq"]] = dict(e, verdict=None)
        req = t["reqs"][e["req"]]
        if e["passed"]:
            _drop_review_requests(s, t, e["req"])
            req["proof"] = e["seq"]
            s.requests[e["seq"]] = {"seq": e["seq"], "type": "review", "task": t["slug"], "to": "driver",
                                    "from": actor, "status": "open", "blocking": True, "proof": e["seq"]}
    elif k in ("accept", "reject"):
        p = s.proofs[e["proof"]]
        p["verdict"] = k
        p["why"] = e.get("why")
        s.requests[e["proof"]]["status"] = "done"
    elif k in ("ask", "propose", "decide"):
        s.requests[e["seq"]] = {
            "seq": e["seq"], "type": k, "task": e.get("task"), "area": e.get("area"), "from": actor,
            "from_role": e.get("from_role"), "to": e.get("to", {"propose": "driver", "decide": "human"}.get(k)),
            "text": e.get("text") or e.get("question"), "blocking": e.get("blocking", False),
            "options": e.get("options"), "option_notes": e.get("option_notes", {}), "recommend": e.get("recommend"), "merge": e.get("merge", False),
            "candidate": e.get("candidate"), "attachments": e.get("attachments", []), "status": "open",
            "answer": None, "settled": False}
        if k == "decide" and e.get("from"):
            s.requests[e["from"]]["status"] = "forwarded"
    elif k == "answer":
        s.requests[e["request"]].update(status="done", answer=e["text"])
    elif k == "decision":
        s.requests[e["request"]].update(status="done", answer={"option": e["option"], "why": e["why"]})
    elif k in ("cancel", "decline"):
        s.requests[e["request"]].update(status="cancelled" if k == "cancel" else "declined", answer=e.get("why"))
    elif k == "settle":
        s.requests[e["request"]]["settled"] = True
    elif k in ("close", "drop"):
        t["status"] = "closed" if k == "close" else "dropped"
        t["closed"] = {k2: e.get(k2) for k2 in ("candidate", "merged", "proofs", "why")}
        for r in s.requests.values():
            if r["task"] == t["slug"] and r["status"] == "open":
                r["status"] = "cancelled"
            if r["task"] == t["slug"] and r["type"] == "decide":
                r["settled"] = True


def _add_req(task, r, seq):
    task["reqs"][r["id"]] = dict(r, rev=seq, waived=None, proof=None)


def _drop_review_requests(s, task, req_id):
    for r in s.requests.values():
        if r["type"] == "review" and r["status"] == "open" and r["task"] == task["slug"] \
                and s.proofs[r["proof"]]["req"] == req_id:
            r["status"] = "superseded"


# --- views (state + observations)

def req_status(s, task, req, subject):
    """missing | proved | accepted | rejected | stale | waived, plus the current proof seq."""
    if req["waived"]:
        return "waived", None
    p = s.proofs.get(req["proof"])
    if not p or p["req_rev"] != req["rev"]:
        return "missing", None
    if p["verdict"] == "reject":
        return "rejected", p["seq"]
    if subject is not None and not fresh(p["subject"], subject, req["kind"]):
        return "stale", p["seq"]
    return ("accepted" if p["verdict"] == "accept" else "proved"), p["seq"]


def fresh(proved, current, kind):
    if kind == "tree":
        return proved["tree"] == current["tree"]
    if kind == "commit":
        return proved["commit"] == current["commit"]
    return proved["commit"] == current["commit"] and proved["base"] == current["base"]


def health(panes, block):
    if not block:
        return None
    if panes is None:
        return "unknown"
    return "present" if block in panes else "absent"


def progress(s, task, subject):
    if task["status"] != "open":
        return task["status"]
    lead = task["assign"].get("lead")
    if not lead:
        return "unassigned"
    if not lead["block"]:
        return "assigned"
    states = [req_status(s, task, r, subject)[0] for r in task["reqs"].values()]
    if any(x in ("missing", "rejected", "stale") for x in states):
        return "working"
    if any(x == "proved" for x in states):
        return "validating"
    if any(r["type"] == "decide" and r["merge"] and r["status"] == "open" for r in task_requests(s, task)):
        return "awaiting-merge"
    return "ready"


def task_requests(s, task):
    return [r for r in s.requests.values() if r["task"] == task["slug"]]


def blockers(s, task):
    return [r["seq"] for r in task_requests(s, task) if r["status"] == "open" and r["blocking"]
            and r["type"] in ("ask", "decide")]


def recipient(s, r):
    """The actor a request waits on now: driver, human, or the current holder of a task role."""
    if r["to"] in ("driver", "human"):
        return r["to"]
    task = s.tasks.get(r["task"])
    return task and s.holder(task, r["to"])


def asker(s, r):
    if r.get("from_role") in ROLES and r["task"] in s.tasks:
        return s.holder(s.tasks[r["task"]], r["from_role"])
    return r["from"]


def inbox(s, actor):
    return [r for r in s.requests.values() if r["status"] == "open" and recipient(s, r) == actor]
