"""Human-in-the-loop UAT: you paste prompts into Microsoft 365 Copilot.

Skipped by `unittest discover` unless BBAGENT_HITL is set. Run it yourself:

    python tests/test_hitl.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bbagent import cli, session, tools

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_bug"
HITL_LAST = ROOT / ".bbagent-hitl-last"
CONTRACT = (
    "UniqueContextToken: answer() in widget.py must return 2.\n"
    "Do not edit this spec file.\n"
)


def _hitl_enabled() -> bool:
    return os.environ.get("BBAGENT_HITL", "").strip().lower() in {"1", "true", "yes"}


def _load_widget(root: Path):
    path = root / "widget.py"
    spec = importlib.util.spec_from_file_location("hitl_widget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CopilotPasteHitlTests(unittest.TestCase):
    def setUp(self):
        if not _hitl_enabled():
            self.skipTest("set BBAGENT_HITL=1 or run python tests/test_hitl.py")
        if not sys.stdin.isatty():
            self.skipTest("run this test in a terminal so clipboard paste works")

    def test_user_fixes_tiny_bug_via_copilot_paste(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "proj"
        spec_dir = Path(tmp.name) / "spec"
        shutil.copytree(FIXTURE, root)
        spec_dir.mkdir()
        contract = spec_dir / "contract.md"
        contract.write_text(CONTRACT, encoding="utf-8", newline="\n")

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
        config["max_turns"] = 12
        config["context_dirs"] = [str(spec_dir)]
        session.save_config(root, config)

        task = "Follow UniqueContextToken and make answer() return 2."
        print()
        print("Human-in-the-loop Copilot paste test")
        print("====================================")
        print(f"Workspace: {root}")
        print(f"Task: {task}")
        print("widget.py currently returns 1. The check expects 2.")
        print("A related spec is attached as context (@spec/contract.md).")
        print()
        print("Each turn:")
        print("  1. The prompt is on the clipboard and in .bbagent/prompt.md")
        print("  2. Paste it into a fresh Microsoft 365 Copilot chat")
        print("  3. Copy the entire reply")
        print("  4. Press Enter here")
        print()
        print("Stop when Copilot emits done and the check passes.")
        print("Ctrl+C saves the session and fails this test.")
        print()
        try:
            input("Press Enter to start. ")
        except EOFError:
            self.fail("stdin closed before the paste loop started")

        code = cli.main([task, "--root", str(root)])
        state = session.load_session(root)
        finished = bool(state and state.get("finished"))
        if code != 0 or not finished:
            self._fail_hitl(root, code, state)

        self.assertEqual(_load_widget(root).answer(), 2)
        check_code, check_output = tools.run_cmd(root, config["test_command"])
        self.assertEqual(check_code, 0, check_output)
        self.assertEqual(contract.read_text(encoding="utf-8"), CONTRACT)

    def _fail_hitl(self, root: Path, code: int, state: dict | None) -> None:
        dest = HITL_LAST
        if dest.exists():
            shutil.rmtree(dest)
        src = root / ".bbagent"
        if src.is_dir():
            shutil.copytree(src, dest)
            print(f"HITL artifacts copied to {dest}")
        preview = ""
        transcript = session.transcript_path(root)
        if transcript.is_file():
            lines = transcript.read_text(encoding="utf-8").splitlines()
            if lines:
                record = json.loads(lines[-1])
                preview = (record.get("reply") or "")[:800]
        reason = {
            130: "Session saved unfinished. Finish the Copilot paste loop, then re-run.",
            1: "Stopped before done. Copilot replies were not executed.",
        }.get(code, f"bbagent exited {code} without solving the task.")
        parts = [
            reason,
            f"See {dest / 'transcript.jsonl'}",
        ]
        if preview:
            parts.append("Last reply preview:\n" + preview)
        if state and state.get("gate"):
            parts.append("Last gate: " + str(state["gate"]).splitlines()[0][:200])
        self.fail("\n".join(parts))


if __name__ == "__main__":
    os.environ["BBAGENT_HITL"] = "1"
    if not sys.stdin.isatty():
        print("Run this test in a terminal so clipboard paste works.", file=sys.stderr)
        raise SystemExit(2)
    unittest.main()
