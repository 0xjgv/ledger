import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledger.cli import main  # noqa: E402
from ledger.world import World  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
os.environ.update(GIT_ENV)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Fake(World):
    def __init__(self, bed, cwd, block, address):
        super().__init__(cwd=str(cwd), env={})
        self.bed, self._block, self.address = bed, block, address

    def block(self):
        return self._block

    def panes(self):
        return None if self.bed.rex_down else dict(self.bed.panes)

    def reason_address(self):
        return self.address

    def ring(self, to, text, fyi=False, notify=False):
        self.bed.rings.append((to, text, fyi, notify))


class Bed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.wt = self.tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "feat", str(self.wt))
        self.panes = {"b-driver": None, "b-lead": "rex-agents:o:lead-a", "b-rev": "rex-agents:o:rev-a"}
        self.rex_down = False
        self.rings = []

    def run_as(self, who, *argv, cwd=None, expect=0):
        block, addr, default = {"driver": ("b-driver", "me/driver", self.repo),
                                "lead": ("b-lead", "me/lead", self.wt),
                                "rev": ("b-rev", "me/rev", self.wt),
                                "human": (None, None, self.repo)}[who] if isinstance(who, str) else who
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv), Fake(self, cwd or default, block, addr))
        data = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        self.assertEqual(code, expect, data)
        return data

    def commit(self, name="b.txt", text="b\n", msg="work"):
        (self.wt / name).write_text(text)
        git(self.wt, "add", ".")
        git(self.wt, "commit", "-qm", msg)

    def setup_task(self, merge=True):
        self.run_as("driver", "init")
        argv = ["new", "t1", "Fix it", "--req", "tests:lead:tree:tests pass", "--req", "review:reviewer:diff:review PASS"]
        self.run_as("driver", *argv + (["--merge"] if merge else []))
        self.run_as("driver", "assign", "t1", "lead-a", "--branch", "feat", "--cwd", str(self.wt))
        self.run_as("lead", "start", "t1")
        self.commit()

    def prove_tests(self):
        return self.run_as("lead", "prove", "t1", "tests", "--", "true")["event"]["seq"]


class HappyPath(Bed):
    def test_full_flow_to_close(self):
        self.setup_task()
        p1 = self.prove_tests()
        self.assertEqual(self.run_as("driver", "next")["action"], "validate")
        self.run_as("driver", "accept", str(p1))
        self.assertEqual(self.run_as("driver", "next")["action"], "assign")
        self.run_as("driver", "assign", "t1", "rev-a", "--role", "reviewer")
        self.run_as("rev", "start", "t1")
        review = self.tmp / "review.md"
        review.write_text("---\nverdict: PASS\n---\nok\n")
        p2 = self.run_as("rev", "prove", "t1", "review", "--file", str(review))["event"]["seq"]
        self.run_as("driver", "accept", str(p2))
        nxt = self.run_as("driver", "next")
        self.assertEqual(nxt["action"], "decide-merge")
        d = self.run_as("driver", "decide", "t1", "Merge t1?", "--merge", "--recommend", "merge")["event"]
        self.assertEqual(self.run_as("human", "inbox", "--human")["requests"][0]["seq"], d["seq"])
        self.run_as("human", "decision", str(d["seq"]), "merge", "--human", "--why", "looks good")
        nxt = self.run_as("driver", "next")
        self.assertEqual(nxt["action"], "merge")
        show = self.run_as("driver", "show", "t1")
        self.run_as("driver", "close", "t1", "--candidate", show["candidate"], "--rev", str(show["rev"]))
        self.assertEqual(self.run_as("driver", "show", "t1")["status"], "closed")
        self.assertEqual(self.run_as("driver", "next")["action"], "idle")

    def test_lead_next_and_doorbell(self):
        self.setup_task()
        self.assertEqual(self.run_as("lead", "next")["action"], "prove")
        self.prove_tests()
        self.assertIn("me/driver", [r[0] for r in self.rings])
        self.assertEqual(self.run_as("lead", "next")["action"], "idle")


class Wakeups(Bed):
    def test_own_pending_proof_does_not_idle_the_lead(self):
        self.setup_task()
        self.run_as("driver", "require", "t1", "demo:lead:tree:demo shown")
        self.prove_tests()
        nxt = self.run_as("lead", "next")
        self.assertEqual((nxt["action"], "demo" in nxt["commands"][0]), ("prove", True))

    def test_accept_wakes_prover_and_reviewer_when_lead_work_done(self):
        self.setup_task()
        self.run_as("driver", "assign", "t1", "rev-a", "--role", "reviewer")
        self.run_as("rev", "start", "t1")
        self.assertEqual(self.run_as("rev", "next")["action"], "idle")
        p = self.prove_tests()
        self.rings.clear()
        self.run_as("driver", "accept", str(p))
        self.assertEqual(sorted((r[0], r[2]) for r in self.rings), [("me/lead", False), ("me/rev", False)])
        self.assertEqual(self.run_as("rev", "next")["action"], "prove")


class Proofs(Bed):
    def test_dirty_worktree_refused(self):
        self.setup_task()
        (self.wt / "c.txt").write_text("x")
        self.assertEqual(self.run_as("lead", "prove", "t1", "tests", "--", "true", expect=2)["code"], "dirty")

    def test_failed_command_is_an_attempt(self):
        self.setup_task()
        e = self.run_as("lead", "prove", "t1", "tests", "--", "false")["event"]
        self.assertFalse(e["passed"])
        self.assertEqual(self.run_as("driver", "show", "t1")["requirements"]["tests"]["status"], "missing")

    def test_new_commit_makes_proof_stale_and_accept_refuses(self):
        self.setup_task()
        p1 = self.prove_tests()
        self.commit("c.txt", "c\n")
        self.assertEqual(self.run_as("driver", "show", "t1")["requirements"]["tests"]["status"], "stale")
        self.assertEqual(self.run_as("driver", "accept", str(p1), expect=2)["code"], "stale_proof")

    def test_tree_proof_survives_same_content_commit(self):
        self.setup_task()
        self.prove_tests()
        git(self.wt, "commit", "--amend", "-qm", "reworded")
        self.assertEqual(self.run_as("driver", "show", "t1")["requirements"]["tests"]["status"], "proved")

    def test_accept_of_replaced_proof_refused(self):
        self.setup_task()
        p1 = self.prove_tests()
        self.prove_tests()
        self.assertEqual(self.run_as("driver", "accept", str(p1), expect=2)["code"], "replaced_proof")

    def test_review_file_without_pass_fails(self):
        self.setup_task()
        self.run_as("driver", "assign", "t1", "rev-a", "--role", "reviewer")
        self.run_as("rev", "start", "t1")
        review = self.tmp / "review.md"
        review.write_text("---\nverdict: FAIL\n---\n")
        self.assertFalse(self.run_as("rev", "prove", "t1", "review", "--file", str(review))["event"]["passed"])

    def test_lead_cannot_prove_reviewer_requirement(self):
        self.setup_task()
        review = self.tmp / "review.md"
        review.write_text("x")
        self.assertEqual(self.run_as("lead", "prove", "t1", "review", "--file", str(review), expect=2)["code"],
                         "wrong_role")


class Close(Bed):
    def test_close_refuses_unready_wrong_rev_and_missing_merge_decision(self):
        self.setup_task()
        show = self.run_as("driver", "show", "t1")
        self.assertEqual(self.run_as("driver", "close", "t1", "--candidate", show["candidate"], "--rev",
                                     str(show["rev"]), expect=2)["code"], "not_ready")
        self.run_as("driver", "accept", str(self.prove_tests()))
        self.run_as("driver", "waive", "t1", "review", "not needed")
        show = self.run_as("driver", "show", "t1")
        self.assertEqual(self.run_as("driver", "close", "t1", "--candidate", show["candidate"], "--rev",
                                     str(show["rev"] - 1), expect=2)["code"], "stale_rev")
        self.assertEqual(self.run_as("driver", "close", "t1", "--candidate", show["candidate"], "--rev",
                                     str(show["rev"]), expect=2)["code"], "no_merge_decision")

    def test_task_without_merge_closes_when_ready(self):
        self.setup_task(merge=False)
        self.run_as("driver", "accept", str(self.prove_tests()))
        self.run_as("driver", "waive", "t1", "review", "not needed")
        nxt = self.run_as("driver", "next")
        self.assertEqual(nxt["action"], "close")
        self.run_as("driver", *nxt["commands"][0].split()[1:])

    def test_blocking_ask_blocks_close(self):
        self.setup_task(merge=False)
        self.run_as("driver", "accept", str(self.prove_tests()))
        self.run_as("driver", "waive", "t1", "review", "not needed")
        self.run_as("driver", "ask", "t1", "Any risk?", "--blocking")
        show = self.run_as("driver", "show", "t1")
        self.assertEqual(self.run_as("driver", "close", "t1", "--candidate", show["candidate"], "--rev",
                                     str(show["rev"]), expect=2)["code"], "blocked")


class Identity(Bed):
    def test_only_driver_creates_tasks(self):
        self.run_as("driver", "init")
        self.assertEqual(self.run_as("lead", "new", "t2", "x", "--req", "a:lead:tree:x", expect=2)["code"], "not_driver")

    def test_driver_takeover_only_when_old_pane_absent(self):
        self.run_as("driver", "init")
        newpane = ("b-new", "me/driver2", self.repo)
        self.panes["b-new"] = None
        self.assertEqual(self.run_as(newpane, "init", expect=2)["code"], "driver_alive")
        self.rex_down = True
        self.assertEqual(self.run_as(newpane, "init", expect=2)["code"], "driver_alive")
        self.rex_down = False
        del self.panes["b-driver"]
        self.run_as(newpane, "init")
        self.run_as(newpane, "new", "t2", "x", "--req", "a:lead:tree:x")

    def test_worker_cannot_take_over_driver(self):
        self.run_as("driver", "init")
        del self.panes["b-driver"]
        self.assertEqual(self.run_as("lead", "init", expect=2)["code"], "worker_pane")

    def test_human_rebind(self):
        self.run_as("driver", "init")
        self.panes["b-new"] = None
        self.run_as(("b-new", "me/d2", self.repo), "rebind", "--human", "--why", "restart")
        self.assertEqual(self.run_as("driver", "new", "t2", "x", "--req", "a:lead:tree:x", expect=2)["code"], "not_driver")

    def test_start_requires_matching_label_and_absent_old_pane(self):
        self.setup_task()
        self.assertEqual(self.run_as("rev", "start", "t1", expect=2)["code"], "not_assigned")
        self.panes["b-lead2"] = "rex-agents:o:lead-a"
        relaunched = ("b-lead2", "me/lead2", self.wt)
        self.assertEqual(self.run_as(relaunched, "start", "t1", expect=2)["code"], "bound_elsewhere")
        del self.panes["b-lead"]
        self.run_as(relaunched, "start", "t1")
        self.assertEqual(self.run_as(relaunched, "next")["action"], "prove")


class Requests(Bed):
    def test_ask_moves_to_new_lead_and_old_lead_stops(self):
        self.setup_task()
        ask = self.run_as("driver", "ask", "t1", "Status?")["event"]["seq"]
        self.panes["b-lead2"] = "rex-agents:o:lead-b"
        self.run_as("driver", "assign", "t1", "lead-b", "--branch", "feat", "--cwd", str(self.wt))
        self.assertEqual(self.run_as("lead", "next")["action"], "stop")
        lead_b = ("b-lead2", "me/leadb", self.wt)
        self.run_as(lead_b, "start", "t1")
        self.assertEqual(self.run_as(lead_b, "next")["request"], ask)
        self.run_as(lead_b, "answer", str(ask), "fine")

    def test_decision_conflict_refused_and_repeat_harmless(self):
        self.run_as("driver", "init")
        d = self.run_as("driver", "decide", "Ship?", "--option", "yes", "--option", "no")["event"]["seq"]
        self.assertEqual(self.run_as("human", "decision", str(d), "yes", expect=2)["code"], "human_only")
        self.run_as("human", "decision", str(d), "yes", "--human", "--why", "ok")
        self.assertTrue(self.run_as("human", "decision", str(d), "yes", "--human", "--why", "ok")["repeated"])
        self.assertEqual(self.run_as("human", "decision", str(d), "no", "--human", "--why", "x", expect=2)["code"],
                         "conflict")
        self.assertEqual(self.run_as("driver", "next")["action"], "act")
        self.run_as("driver", "settle", str(d))
        self.assertEqual(self.run_as("driver", "next")["action"], "idle")

    def test_idea_forwarded_to_human(self):
        self.setup_task()
        idea = self.run_as("lead", "propose", "t1", "Cache it")["event"]["seq"]
        self.assertEqual([r["seq"] for r in self.run_as("driver", "inbox")["requests"]], [idea])
        d = self.run_as("driver", "decide", "Cache?", "--option", "yes", "--option", "no", "--from", str(idea))
        self.assertEqual(self.run_as("human", "inbox", "--human")["requests"][0]["seq"], d["event"]["seq"])

    def test_option_descriptions_reach_the_queue(self):
        self.run_as("driver", "init")
        self.run_as("driver", "decide", "Ship?", "--option", "yes: ship today", "--option", "no")
        r = self.run_as("human", "inbox", "--human")["requests"][0]
        self.assertEqual((r["options"], r["option_notes"]), (["yes", "no"], {"yes": "ship today"}))
        self.assertEqual(self.run_as("driver", "decide", "x", "--option", "a b", "--option", "c", expect=2)["code"],
                         "bad_options")

    def test_decide_from_idea_inherits_task(self):
        self.setup_task()
        idea = self.run_as("lead", "propose", "t1", "Cache it")["event"]["seq"]
        e = self.run_as("driver", "decide", "Cache?", "--option", "yes", "--option", "no", "--from", str(idea))["event"]
        self.assertEqual(e["task"], "t1")
        self.assertEqual(self.run_as("driver", "inbox")["requests"], [])

    def test_codex_lead_refused_at_start(self):
        self.run_as("driver", "init")
        self.run_as("driver", "new", "t2", "x", "--req", "a:lead:tree:x")
        self.run_as("driver", "assign", "t2", "lead-a", "--branch", "feat", "--cwd", str(self.wt))
        world = Fake(self, self.wt, "b-lead", "me/lead")
        world.env = {"CODEX_THREAD_ID": "abc"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["start", "t2"], world), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "unsupported_harness")

    def test_request_id_retry(self):
        self.setup_task()
        a = self.run_as("lead", "--request-id", "r1", "ask", "t1", "Port?")["event"]["seq"]
        again = self.run_as("lead", "--request-id", "r1", "ask", "t1", "Port?")
        self.assertTrue(again["repeated"])
        self.assertEqual(again["event"]["seq"], a)
        self.assertEqual(self.run_as("lead", "--request-id", "r1", "ask", "t1", "Other?", expect=2)["code"],
                         "request_id_conflict")


class Store(Bed):
    def test_torn_tail_repaired_and_interior_corruption_refused(self):
        self.run_as("driver", "init")
        log = self.repo / ".git/ledger/events.jsonl"
        with open(log, "ab") as fh:
            fh.write(b'{"seq": 2, "kind": "ne')
        self.run_as("driver", "new", "t2", "x", "--req", "a:lead:tree:x")
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertTrue(events[1]["repaired_torn_bytes"])
        log.write_text(log.read_text().replace("{", "{x", 1))
        self.assertEqual(self.run_as("driver", "board", expect=2)["code"], "corrupt_log")

    def test_hook_injects_next_action(self):
        self.setup_task()
        out = io.StringIO()
        world = Fake(self, self.wt, "b-lead", "me/lead-new")
        with contextlib.redirect_stdout(out), unittest.mock.patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(self.wt)}))):
            main(["_hook", "session-start"], world)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ledger next", ctx)
        self.assertIn('"action": "prove"', ctx)
        self.assertEqual(self.run_as("driver", "log")["events"][-1]["kind"], "hello")


if __name__ == "__main__":
    unittest.main()
