import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ledger import model  # noqa: E402


class Ledger(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {k: v for k, v in os.environ.items() if k != "LEDGER_AS"}

    def run_l(self, *argv, who=None, ok=True):
        env = dict(self.env, LEDGER_AS=who) if who else self.env
        r = subprocess.run([sys.executable, str(ROOT / "bin/ledger"), *argv], cwd=self.repo, env=env,
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def json(self, *argv, who=None):
        return json.loads(self.run_l(*argv, "--json", who=who).stdout)

    def log(self):
        return [json.loads(line) for line in (self.repo / ".git/ledger/events.jsonl").read_text().splitlines()]

    def test_task_lifecycle(self):
        self.run_l("new", "login", "Fix login", "--lead", "ana")
        self.run_l("set", "login", "state=active", "branch=fix-login", who="ana")
        self.run_l("say", "login", "found the bug", who="ana")
        self.run_l("set", "login", "state=review", who="ana")
        self.run_l("set", "login", "state=done")
        t = self.json("show", "login")
        self.assertEqual((t["state"], t["lead"], t["fields"]), ("done", "ana", {"branch": "fix-login"}))
        self.assertEqual([e["actor"] for e in t["history"]], ["driver", "ana", "ana", "ana", "driver"])
        self.assertEqual(self.json("board")["tasks"], [])
        self.assertEqual(len(self.json("board", "--all")["tasks"]), 1)

    def test_transitions_are_checked(self):
        self.run_l("new", "x", "X")
        r = self.run_l("set", "x", "state=done", ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("x is todo; it can move to: active, dropped.", r.stderr)
        self.assertIn("States:", self.run_l("set", "x", "state=nope", ok=False).stderr)
        self.assertIn("Unknown task", self.run_l("set", "y", "state=active", ok=False).stderr)
        self.assertIn("exists", self.run_l("new", "x", "again", ok=False).stderr)
        self.assertIn("key=value", self.run_l("set", "x", "oops", ok=False).stderr)
        self.assertEqual(len(self.log()), 1)

    def test_field_clears(self):
        self.run_l("new", "x", "X", "--lead", "ana")
        self.run_l("set", "x", "pr=12", "lead=")
        self.run_l("set", "x", "pr=")
        t = self.json("show", "x")
        self.assertEqual((t["lead"], t["fields"]), (None, {}))

    def test_inbox_routes_and_answers(self):
        self.run_l("new", "x", "X", "--lead", "ana")
        self.run_l("ask", "x", "which db?", who="ana")
        self.run_l("ask", "-", "merge?", "--to", "human", "--option", "merge: ship it", "--option", "hold")
        self.assertEqual([i["text"] for i in self.json("inbox")], ["which db?"])
        human = self.json("inbox", "--for", "human")
        self.assertEqual(human[0]["options"], ["merge: ship it", "hold"])
        self.assertEqual(len(self.json("inbox", "--all")), 2)
        self.assertEqual(self.json("board")["inbox"], {"driver": 1, "human": 1})
        self.run_l("answer", "#2", "postgres")
        self.assertIn("is answered: postgres", self.run_l("answer", "2", "again", ok=False).stderr)
        self.assertEqual(self.json("show", "2")["by"], "driver")
        self.assertEqual(self.json("inbox"), [])
        self.assertIn("inbox empty for driver", self.run_l("inbox").stdout)

    def test_ending_a_task_closes_its_items(self):
        self.run_l("new", "x", "X")
        self.run_l("ask", "x", "q?", "--to", "human")
        self.run_l("set", "x", "state=dropped")
        self.assertEqual(self.json("inbox", "--all"), [])
        self.assertIn("is dropped", self.run_l("ask", "x", "late?", ok=False).stderr)

    def test_ask_wait_returns_answer(self):
        self.run_l("new", "x", "X")
        env = dict(self.env, LEDGER_AS="ana")
        p = subprocess.Popen([sys.executable, str(ROOT / "bin/ledger"), "ask", "x", "go?", "--wait", "--json"],
                             cwd=self.repo, env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(50):
            if len(self.log()) == 2:
                break
            subprocess.run(["sleep", "0.1"])
        self.run_l("answer", "2", "yes")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(json.loads(out)["answer"], "yes")

    def test_inbox_wait_times_out(self):
        r = self.run_l("inbox", "--wait", "--timeout", "0")
        self.assertIn("inbox empty", r.stdout)

    def test_bad_names_refused(self):
        self.assertIn("Bad name", self.run_l("board", "--as", "Bad Name", ok=False).stderr)
        self.assertIn("lowercase", self.run_l("new", "Bad", "X", ok=False).stderr)
        r = self.run_l("new", "Bad", "X", "--json", ok=False)
        self.assertEqual(json.loads(r.stderr)["error"], "bad_id")

    def test_torn_tail_repaired_and_gap_refused(self):
        self.run_l("new", "x", "X")
        log = self.repo / ".git/ledger/events.jsonl"
        with open(log, "ab") as fh:
            fh.write(b'{"seq": 2, "half')
        self.run_l("new", "y", "Y")
        self.assertEqual([e["seq"] for e in self.log()], [1, 2])
        log.write_text(log.read_text().replace('"seq": 2', '"seq": 5'))
        self.assertIn("sequence gap", self.run_l("board", ok=False).stderr)

    def test_hook(self):
        self.run_l("new", "x", "X", "--lead", "ana")
        self.run_l("ask", "x", "q?", "--to", "ana")
        hook = [sys.executable, str(ROOT / "bin/ledger"), "_hook", "session-start"]
        payload = json.dumps({"cwd": str(self.repo)})
        r = subprocess.run(hook, input=payload, env=dict(self.env, LEDGER_AS="ana"), capture_output=True, text=True)
        text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("you are ana. Your tasks: x (todo). Open inbox items for you: 1", text)
        r = subprocess.run(hook, input=payload, env=self.env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


def v2(seq, kind, **fields):
    return {"seq": seq, "at": "2026-11-09T00:00:00Z", "kind": kind, **fields}


class Migration(unittest.TestCase):
    def test_v2_events_fold(self):
        s = model.fold([
            v2(1, "init", session="proc:1:x"),
            v2(2, "new", actor="driver", task="t", title="T", reqs=[]),
            v2(3, "assign", actor="driver", task="t", role="lead", name="sol"),
            v2(4, "assign", actor="driver", task="t", role="reviewer", name="rev"),
            v2(5, "start", actor="sol", task="t", role="lead"),
            v2(6, "ask", actor="sol", task="t", to="driver", text="ok?", blocking=True),
            v2(7, "answer", actor="driver", request=6, text="yes"),
            v2(8, "propose", actor="sol", task="t", text="idea"),
            v2(9, "decide", actor="driver", task="t", question="which?", options=["a", "b"],
               option_notes={"a": "first"}, **{"from": 8}),
            v2(10, "ask", actor="driver", task="t", to="lead", text="status?"),
            v2(11, "prove", actor="sol", task="t", req="tests", passed=True),
            v2(12, "decision", actor="human", request=9, option="a", why="simpler"),
        ])
        t = s.tasks["t"]
        self.assertEqual((t["state"], t["lead"], t["fields"]), ("active", "sol", {"reviewer": "rev"}))
        self.assertEqual(s.items[6]["answer"], "yes")
        self.assertEqual(s.items[8]["answer"], "forwarded as #9")
        self.assertEqual((s.items[9]["to"], s.items[9]["options"], s.items[9]["answer"]),
                         ("human", ["a: first", "b"], "a: simpler"))
        self.assertEqual([i["n"] for i in s.inbox()], [10])
        self.assertEqual(s.items[10]["to"], "sol")
        s = model.fold([v2(1, "new", task="t", title="T", reqs=[]), v2(2, "close", task="t")])
        self.assertEqual(s.tasks["t"]["state"], "done")


if __name__ == "__main__":
    unittest.main()
