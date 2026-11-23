import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from bbagent import cli, loop, session

COPILOT_M365 = Path(__file__).parent / "fixtures" / "copilot_replies" / "m365_stripped_fences.md"

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

    def test_m365_stripped_copy_fixes_the_fixture(self):
        root = _project(self)
        reply = COPILOT_M365.read_text(encoding="utf-8")

        def replier(_prompt):
            return reply

        code = loop.run(
            root,
            session.load_session(root),
            replier,
            writer=lambda _text: None,
            confirmer=lambda _message: False,
        )
        self.assertEqual(code, 0)
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))
        self.assertTrue(session.load_session(root)["finished"])

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

    def test_max_turns_returns_one(self):
        root = _project(self)
        config = session.load_config(root)
        config["max_turns"] = 2
        session.save_config(root, config)
        logs = []

        def replier(_prompt):
            return "Happy to help, here is some prose with no action."

        code = loop.run(
            root,
            session.load_session(root),
            replier,
            writer=logs.append,
            confirmer=lambda _message: False,
        )
        self.assertEqual(code, 1)
        self.assertFalse(session.load_session(root).get("finished"))
        self.assertTrue(any(item.startswith("Turn 1:") for item in logs))
        self.assertTrue(any("[FAIL] unparsed reply" in item for item in logs))


class ContextLoopTests(unittest.TestCase):
    def test_extra_dir_enriches_first_prompt(self):
        root = _project(self)
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(extra, ignore_errors=True))
        (extra / "contract.md").write_text(
            "UniqueContextToken: answer() must return 2.\n",
            encoding="utf-8",
            newline="\n",
        )
        state = session.load_session(root)
        state["task"] = "Follow UniqueContextToken and make answer() return 2."
        state["context_dirs"] = [str(extra)]
        session.save_session(root, state)
        seen = []

        def replier(prompt):
            seen.append(prompt)
            if len(seen) > 4:
                raise AssertionError("too many turns\n" + prompt[:400])
            if len(seen) == 1:
                self.assertIn("CONTEXT SOURCES", prompt)
                self.assertIn("UniqueContextToken", prompt)
                self.assertIn("read-only", prompt)
                self.assertIn("@" + extra.name, prompt)
                return REPLACE
            return DONE

        code = loop.run(
            root,
            session.load_session(root),
            replier,
            writer=lambda _text: None,
            confirmer=lambda _message: False,
        )
        self.assertEqual(code, 0, seen[-1] if seen else "")
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))

    def test_context_write_is_refused_and_workspace_still_edits(self):
        root = _project(self)
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(extra, ignore_errors=True))
        (extra / "contract.md").write_text("keep me\n", encoding="utf-8", newline="\n")
        state = session.load_session(root)
        state["context_dirs"] = [str(extra)]
        session.save_session(root, state)
        seen = []
        alias = extra.name
        write_ctx = (
            "```bbagent\n"
            "action: write\n"
            f"path: @{alias}/contract.md\n"
            "```\n"
            "hacked\n"
        )

        def replier(prompt):
            seen.append(prompt)
            if len(seen) > 5:
                raise AssertionError("too many turns\n" + prompt[:500])
            if len(seen) == 1:
                return write_ctx
            if len(seen) == 2:
                self.assertIn("read-only", prompt.lower())
                return REPLACE
            return DONE

        code = loop.run(
            root,
            session.load_session(root),
            replier,
            writer=lambda _text: None,
            confirmer=lambda _message: False,
        )
        self.assertEqual(code, 0)
        self.assertEqual((extra / "contract.md").read_text(encoding="utf-8"), "keep me\n")
        self.assertIn("return 2", (root / "widget.py").read_text(encoding="utf-8"))

    def test_missing_context_dir_stops_the_loop(self):
        root = _project(self)
        state = session.load_session(root)
        state["context_dirs"] = [str(root / "missing-context")]
        session.save_session(root, state)
        messages = []
        code = loop.run(
            root,
            session.load_session(root),
            replier=lambda _prompt: None,
            writer=messages.append,
            confirmer=lambda _message: False,
        )
        self.assertEqual(code, 2)
        self.assertTrue(any("does not exist" in item for item in messages))

    def test_status_lists_context_dirs(self):
        root = _project(self)
        extra = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(extra, ignore_errors=True))
        extra.mkdir(exist_ok=True)
        state = session.load_session(root)
        state["context_dirs"] = [str(extra)]
        session.save_session(root, state)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--status", "--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("context:", stdout.getvalue())
        self.assertIn(str(extra), stdout.getvalue())

    def test_cli_rejects_missing_context_without_a_session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["fix it", "--root", str(root), "--context", str(root / "missing")])
        self.assertEqual(code, 2)
        self.assertIsNone(session.load_session(root))
        self.assertIn("does not exist", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
