import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from bbagent import cli, loop, session

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_bug"

REPLACE = """I'll update the return value.
```bbagent
action: replace
path: widget.py
```
<<<<<<< SEARCH
    return 1
=======
    return 2
>>>>>>> REPLACE
"""

DONE = """```bbagent
action: done
summary: answer now returns 2
```
"""


def _project(test: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "proj"
    shutil.copytree(FIXTURE, root)
    session.new_session(root, "Make answer() return 2.")
    config = session.load_config(root)
    config["test_command"] = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        ".",
        "-p",
        "check_*.py",
    ]
    config["max_turns"] = 8
    session.save_config(root, config)
    return root


class LoopTests(unittest.TestCase):
    def test_scripted_replies_fix_the_fixture(self):
        root = _project(self)
        seen = []

        def replier(prompt):
            seen.append(prompt)
            if len(seen) > 4:
                raise AssertionError("too many turns\n" + prompt[:400])
            if len(seen) == 1:
                return REPLACE
            return DONE

        code = loop.run(root, session.load_session(root), replier, writer=lambda _text: None, confirmer=lambda _message: False)
        self.assertEqual(code, 0, seen[-1] if seen else "")
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))
        saved = session.load_session(root)
        self.assertTrue(saved["finished"])
        self.assertTrue(saved["last_tests_passed"])

    def test_done_is_rejected_while_tests_fail(self):
        root = _project(self)
        seen = []

        def replier(prompt):
            seen.append(prompt)
            if len(seen) > 5:
                raise AssertionError("too many turns\n" + prompt[:500])
            if len(seen) == 1:
                return DONE
            if len(seen) == 2:
                self.assertIn("rejected", prompt.lower())
                return REPLACE
            return DONE

        code = loop.run(root, session.load_session(root), replier, writer=lambda _text: None, confirmer=lambda _message: False)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(seen), 3)
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))

    def test_missing_fence_prompts_a_repair(self):
        root = _project(self)
        seen = []

        def replier(prompt):
            seen.append(prompt)
            if len(seen) > 5:
                raise AssertionError("too many turns\n" + prompt[:500])
            if len(seen) == 1:
                return "Happy to help with your task."
            if len(seen) == 2:
                self.assertIn("nothing was executed", prompt)
                return REPLACE
            return DONE

        code = loop.run(root, session.load_session(root), replier, writer=lambda _text: None, confirmer=lambda _message: False)
        self.assertEqual(code, 0)
        self.assertNotIn("nothing was executed", seen[0])
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))

    def test_unfinished_session_blocks_a_new_task(self):
        root = _project(self)

        def replier(_prompt):
            return None

        code = loop.run(root, session.load_session(root), replier, writer=lambda _text: None)
        self.assertEqual(code, 130)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = cli.main(["--status", "--root", str(root)])
            refused = cli.main(["do something else", "--root", str(root)])
        self.assertEqual(status, 0)
        self.assertIn("turn: 0/8", stdout.getvalue())
        self.assertEqual(refused, 2)


if __name__ == "__main__":
    unittest.main()
