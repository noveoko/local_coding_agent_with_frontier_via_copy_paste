"""Command line for the Copilot paste loop."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from bbagent import clipboard, editing, loop, retrieve, session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bbagent",
        description="Coding agent whose only model is Microsoft 365 Copilot, via copy and paste.",
    )
    parser.add_argument("task", nargs="?", help="Task to solve.")
    parser.add_argument("--resume", action="store_true", help="Continue the saved session.")
    parser.add_argument("--undo", action="store_true", help="Restore the newest file snapshot.")
    parser.add_argument("--status", action="store_true", help="Show the saved session.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Workspace to edit. Defaults to the current directory.")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="DIR",
        help="Related directory to retrieve into the prompt. Repeatable.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    chosen = sum([bool(args.resume), bool(args.undo), bool(args.status)])
    if chosen > 1:
        print("Use only one of --resume, --undo, or --status.", file=sys.stderr)
        return 2
    if args.undo:
        print(editing.undo(root))
        return 0
    if args.status:
        return _status(root)
    if args.resume:
        return _resume(root, args.context)
    if not args.task:
        parser.print_help()
        if session.load_session(root):
            print()
            return _status(root)
        return 2
    return _start(root, args.task, args.context)


def _resolved_context_dirs(root: Path, extra: list[str] | None, existing: list[str] | None = None) -> list[str] | None:
    config = session.load_config(root)
    dirs = session.merge_dirs(config.get("context_dirs") or [], existing or [], extra or [])
    try:
        retrieve.build_sources(root, dirs)
    except retrieve.ContextError as exc:
        print(exc, file=sys.stderr)
        return None
    return dirs


def _start(root: Path, task: str, context_dirs: list[str] | None = None) -> int:
    existing = session.load_session(root)
    if existing and not existing.get("finished"):
        print("An unfinished session exists. Run: python -m bbagent --resume")
        return 2
    if existing and existing.get("finished"):
        _clear_finished(root)
        print("Previous finished session was closed.")
    if _resolved_context_dirs(root, context_dirs) is None:
        return 2
    state = session.new_session(root, task, context_dirs=context_dirs)
    return loop.run(root, state, replier=_interactive_replier(root))


def _resume(root: Path, context_dirs: list[str] | None = None) -> int:
    state = session.load_session(root)
    if state is None:
        print("No session to resume.")
        return 2
    if state.get("finished"):
        print("That session is already finished.")
        return 0
    extra = context_dirs or []
    dirs = _resolved_context_dirs(root, extra, state.get("context_dirs") or [])
    if dirs is None:
        return 2
    if extra:
        state["context_dirs"] = dirs
        session.save_session(root, state)
    return loop.run(root, state, replier=_interactive_replier(root))


def _status(root: Path) -> int:
    state = session.load_session(root)
    if state is None:
        print("No session.")
        return 0
    config = session.load_config(root)
    print(f"task: {state['task']}")
    print(f"turn: {state['turn']}/{config['max_turns']}")
    print(f"finished: {'yes' if state.get('finished') else 'no'}")
    files = ", ".join(state.get("files_touched") or []) or "nothing"
    print(f"files: {files}")
    dirs = state.get("context_dirs") or config.get("context_dirs") or []
    if dirs:
        print("context: " + ", ".join(str(item) for item in dirs))
    if state.get("plan"):
        print("plan: " + state["plan"].splitlines()[0])
    if state.get("gate"):
        print("gate: " + state["gate"].splitlines()[0])
    return 0


def _clear_finished(root: Path) -> None:
    snaps = root / ".bbagent" / "snapshots"
    if snaps.exists():
        shutil.rmtree(snaps)
    transcript = session.transcript_path(root)
    if transcript.exists():
        transcript.unlink()


def _reply_stamp(path: Path) -> tuple[str | None, float | None]:
    if not path.is_file():
        return None, None
    try:
        return path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime
    except OSError:
        return None, None


def _interactive_replier(root: Path):
    piped = {"used": False}

    def replier(prompt: str) -> str | None:
        count = len(prompt)
        prompt_file = session.prompt_path(root)
        before_clip = clipboard.get_text() if clipboard.available() else None
        copied = clipboard.set_text(prompt)
        if copied:
            print(f"Prompt copied to the clipboard ({count} chars). Also written to {prompt_file}.")
        else:
            print(f"Prompt written to {prompt_file} ({count} chars). Clipboard is unavailable.")
        print("1. Paste into Microsoft 365 Copilot.")
        print("2. Copy the entire reply.")
        print("3. Press Enter here.")
        reply_file = session.reply_path(root)
        previous_body, previous_mtime = _reply_stamp(reply_file)
        if not sys.stdin.isatty():
            if piped["used"]:
                return None
            piped["used"] = True
            data = sys.stdin.read()
            return data if data.strip() else None
        while True:
            try:
                input()
            except EOFError:
                return None
            clip = clipboard.get_text() if clipboard.available() else None
            if clip and clip.strip() == prompt.strip():
                print("That is still the prompt. Copy the Copilot reply, then press Enter.")
                continue
            if clip and clip.strip():
                if not copied and before_clip is not None and clip == before_clip:
                    print("Clipboard has not changed. Copy the Copilot reply, then press Enter.")
                    continue
                print(f"Accepted clipboard reply ({len(clip)} chars).")
                return clip
            if reply_file.is_file():
                body = reply_file.read_text(encoding="utf-8", errors="replace")
                try:
                    mtime = reply_file.stat().st_mtime
                except OSError:
                    mtime = None
                fresh = body != previous_body or (
                    previous_mtime is not None and mtime is not None and mtime > previous_mtime
                )
                if previous_body is None:
                    fresh = True
                if body.strip() and body.strip() != prompt.strip() and fresh:
                    print(f"Accepted reply.md ({len(body)} chars).")
                    return body
            print("Clipboard is empty. Copy the reply, or save it as .bbagent/reply.md, then press Enter.")

    return replier
