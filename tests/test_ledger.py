import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ledger import model  # noqa: E402


class Ledger(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("LEDGER_AS", "LEDGER_DIR")}

    def test_env_strips_ledger_vars(self):
        with unittest.mock.patch.dict(os.environ, LEDGER_AS="ana", LEDGER_DIR="/live/store"):
            self.setUp()
        self.assertFalse({"LEDGER_AS", "LEDGER_DIR"} & self.env.keys())

    def run_l(self, *argv, who=None, ok=True, cwd=None):
        env = dict(self.env, LEDGER_AS=who) if who else self.env
        r = subprocess.run([sys.executable, str(ROOT / "bin/ledger"), *argv], cwd=cwd or self.repo, env=env,
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

    def test_same_state_is_allowed(self):
        self.run_l("new", "x", "X")
        self.run_l("set", "x", "state=active")
        self.run_l("set", "x", "state=active")
        self.assertEqual(self.json("show", "x")["state"], "active")

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

    def test_answers_return_to_the_asker_once(self):
        self.run_l("new", "x", "X")
        self.run_l("ask", "x", "merge?", "--to", "human")
        self.run_l("answer", "2", "yes", who="human")
        r = self.run_l("inbox", "--wait", "--timeout", "0")
        self.assertIn("answered by human: yes", r.stdout)
        self.assertEqual(self.json("inbox"), [])
        self.assertEqual(self.json("inbox", "--all"), [])

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

    def test_inbox_wait_after(self):
        self.run_l("new", "x", "X")
        self.run_l("ask", "x", "old?")
        start = time.monotonic()
        r = self.run_l("inbox", "--wait", "--after", "2", "--timeout", "1")
        self.assertGreaterEqual(time.monotonic() - start, 1)
        self.assertIn("#2 [x] driver → driver", r.stdout)
        self.assertTrue(r.stdout.endswith("head: 2\n"))
        p = subprocess.Popen([sys.executable, str(ROOT / "bin/ledger"), "inbox", "--wait", "--after", "2", "--json"],
                             cwd=self.repo, env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        self.run_l("ask", "x", "new?", who="ana")
        out, _ = p.communicate(timeout=10)
        self.assertEqual([i["text"] for i in json.loads(out)], ["old?", "new?"])

    def test_inbox_wait_after_returns_answers(self):
        self.run_l("new", "x", "X")
        self.run_l("ask", "x", "merge?", "--to", "human")
        self.run_l("answer", "2", "yes", who="human")
        start = time.monotonic()
        r = self.run_l("inbox", "--wait", "--after", "99", "--timeout", "30")
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("answered by human: yes", r.stdout)

    def test_head(self):
        self.assertEqual(self.run_l("inbox").stdout, "inbox empty for driver\nhead: 0\n")
        self.run_l("new", "x", "X")
        self.run_l("say", "x", "hi")
        self.assertTrue(self.run_l("inbox").stdout.endswith("head: 2\n"))
        self.assertEqual(self.json("board")["head"], self.log()[-1]["seq"])
        self.assertIsInstance(self.json("inbox"), list)

    def test_ledger_dir(self):
        store = Path(tempfile.mkdtemp()) / "store"
        self.env["LEDGER_DIR"] = str(store)
        plain = Path(tempfile.mkdtemp())
        self.run_l("new", "x", "X", "--lead", "ana", cwd=plain)
        self.assertTrue((store / "events.jsonl").exists())
        other = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertEqual([t["id"] for t in self.json("board")["tasks"]], ["x"])
        self.assertEqual([t["id"] for t in json.loads(self.run_l("board", "--json", cwd=other).stdout)["tasks"]], ["x"])
        self.assertFalse((self.repo / ".git/ledger").exists() or (other / ".git/ledger").exists())
        hook = [sys.executable, str(ROOT / "bin/ledger"), "_hook", "session-start"]
        r = subprocess.run(hook, input=json.dumps({"cwd": str(other)}), env=dict(self.env, LEDGER_AS="ana"),
                           capture_output=True, text=True)
        self.assertIn("Your tasks: x (todo)", r.stdout)
        for bad in ("rel/store", str(plain / "missing/store")):
            self.env["LEDGER_DIR"] = bad
            r = self.run_l("board", "--json", ok=False)
            self.assertEqual((r.returncode, json.loads(r.stderr)["error"]), (1, "bad_dir"))
        self.assertFalse((plain / "missing").exists())

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
