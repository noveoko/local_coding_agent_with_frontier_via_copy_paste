import tempfile
import unittest
from pathlib import Path

from bbagent.editing import apply_replace, apply_write, match_search, undo
from bbagent.tools import is_denied


def _lines(text: str) -> list[str]:
    return text.splitlines()


class MatchTests(unittest.TestCase):
    def test_exact_and_trailing_whitespace(self):
        lines = ["value = 1   ", "next"]
        exact = match_search(lines, ["value = 1   "])
        self.assertEqual(exact.status, "exact")
        stripped = match_search(lines, ["value = 1"])
        self.assertEqual(stripped.status, "stripped")

    def test_ambiguous_match_is_refused(self):
        matched = match_search(["return 1", "return 1"], ["return 1"])
        self.assertEqual(matched.status, "ambiguous")
        self.assertEqual(matched.count, 2)

    def test_fuzzy_above_threshold(self):
        lines = ["alpha beta gamma delta epsilon zeta eta theta"]
        matched = match_search(lines, ["alpha beta gamma delta epsilon zeta eta thetas"])
        self.assertEqual(matched.status, "fuzzy", matched.ratio)

    def test_near_miss_does_not_count_as_fuzzy(self):
        lines = ["alpha beta gamma delta"]
        matched = match_search(lines, ["zzz yyy xxx www"])
        self.assertIn(matched.status, {"close", "miss"})
        self.assertNotEqual(matched.status, "fuzzy")


class ApplyTests(unittest.TestCase):
    def test_replace_exact_stripped_fuzzy_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "widget.py"
            path.write_text("value = 1   \nnext\n", encoding="utf-8", newline="\n")
            stripped = apply_replace(
                root,
                "widget.py",
                "<<<<<<< SEARCH\nvalue = 1\n=======\nvalue = 2\n>>>>>>> REPLACE\n",
                turn=1,
            )
            self.assertTrue(stripped.ok)
            self.assertIn("trailing whitespace", stripped.message)
            self.assertIn("value = 2", path.read_text(encoding="utf-8"))

            fuzzy = apply_replace(
                root,
                "widget.py",
                "<<<<<<< SEARCH\nalpha beta gamma delta epsilon zeta eta thetas\n=======\nreplaced line\n>>>>>>> REPLACE\n",
                turn=2,
            )
            self.assertFalse(fuzzy.ok)

            path.write_text(
                "alpha beta gamma delta epsilon zeta eta theta\n",
                encoding="utf-8",
                newline="\n",
            )
            fuzzy = apply_replace(
                root,
                "widget.py",
                "<<<<<<< SEARCH\nalpha beta gamma delta epsilon zeta eta thetas\n=======\nreplaced line\n>>>>>>> REPLACE\n",
                turn=3,
            )
            self.assertTrue(fuzzy.ok, fuzzy.message)
            self.assertIn("fuzzy", fuzzy.message)
            self.assertIn("replaced line", path.read_text(encoding="utf-8"))

            path.write_text("return 1\nreturn 1\n", encoding="utf-8", newline="\n")
            ambiguous = apply_replace(
                root,
                "widget.py",
                "<<<<<<< SEARCH\nreturn 1\n=======\nreturn 2\n>>>>>>> REPLACE\n",
                turn=4,
            )
            self.assertFalse(ambiguous.ok)
            self.assertEqual(path.read_text(encoding="utf-8"), "return 1\nreturn 1\n")

    def test_path_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widget.py").write_text("x = 1\n", encoding="utf-8")
            outside = apply_replace(root, "../outside.py", "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n", 1)
            reserved = apply_replace(
                root,
                ".bbagent/session.json",
                "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n",
                1,
            )
            self.assertFalse(outside.ok)
            self.assertFalse(reserved.ok)
            self.assertIn("escapes", outside.message)
            self.assertIn("reserved", reserved.message)
            alias = apply_write(root, "@spec-pack/oracle.py", "hacked\n", turn=1)
            self.assertFalse(alias.ok)
            self.assertIn("read-only", alias.message)

    def test_write_limit_snapshot_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = apply_write(root, "fresh.py", "print(1)\n", turn=1)
            self.assertTrue(created.ok)
            self.assertTrue((root / "fresh.py").is_file())
            big = root / "big.py"
            big.write_text("\n".join(f"line {i}" for i in range(50)) + "\n", encoding="utf-8")
            refused = apply_write(root, "big.py", "tiny\n", turn=2)
            self.assertFalse(refused.ok)
            self.assertGreater(len(big.read_text(encoding="utf-8").splitlines()), 40)
            (root / "fresh.py").write_text("print(1)\n", encoding="utf-8")
            note = undo(root)
            self.assertIn("Restored", note)
            self.assertFalse((root / "fresh.py").exists())
            note = undo(root)
            self.assertIn("Nothing", note)


class DenyTests(unittest.TestCase):
    def test_destructive_commands_are_refused(self):
        self.assertIn("recursive", is_denied(["rm", "-rf", "tree"]))
        self.assertIn("recursive", is_denied(["del", "/s", "/q", "tree"]))
        self.assertIn("push", is_denied(["git", "push", "origin", "main"]))
        self.assertIn("reset", is_denied(["git.exe", "reset", "--hard"]))
        self.assertIn("clean", is_denied(["git", "clean", "-fd"]))
        self.assertIsNone(is_denied(["python", "-m", "unittest"]))


if __name__ == "__main__":
    unittest.main()
