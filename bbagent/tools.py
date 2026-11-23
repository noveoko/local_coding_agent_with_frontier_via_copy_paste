"""Read, list, and run. Commands are argv lists, never a shell string."""

from __future__ import annotations

import subprocess
from pathlib import Path

from bbagent.editing import resolve_path
from bbagent.retrieve import SKIP_DIRS

MAX_READ_LINES = 200
RUN_TIMEOUT = 60


def list_dir(root: Path, rel: str) -> str:
    if rel in {"", "."}:
        path = root.resolve()
        label = "."
    else:
        path = resolve_path(root, rel)
        label = rel
    if not path.exists():
        return f"{label} does not exist"
    if not path.is_dir():
        return f"{label} is not a directory"
    lines = []
    try:
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        return f"cannot list {label}: {exc}"
    for entry in entries:
        if entry.name in SKIP_DIRS:
            continue
        if entry.is_dir():
            lines.append(entry.name + "/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{entry.name}  {size}")
    return "\n".join(lines) if lines else "(empty)"


def read_range(root: Path, rel: str, start: int | None, end: int | None) -> dict:
    from bbagent.retrieve import is_secret

    path = resolve_path(root, rel)
    if is_secret(path):
        return {"error": f"refusing to read secret file {rel}"}
    if not path.is_file():
        return {"error": f"{rel} is not a file"}
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    total = len(lines)
    start_line = 1 if start is None else start
    if start_line < 1:
        start_line = 1
    if start_line > total and total > 0:
        return {"error": f"{rel} has {total} lines; start {start_line} is past the end"}
    if end is None:
        end_line = min(total, start_line + MAX_READ_LINES - 1)
    else:
        end_line = end
    if end_line < start_line:
        return {"error": f"end {end_line} is before start {start_line}"}
    end_line = min(end_line, start_line + MAX_READ_LINES - 1, total)
    if total == 0:
        snippet = ""
        start_line, end_line = 1, 1
    else:
        snippet = "\n".join(lines[start_line - 1 : end_line])
    note = ""
    if end is None and total > end_line:
        note = f"\nTruncated at line {end_line} of {total}. Read a smaller range to see the rest."
    return {
        "path": path.relative_to(root.resolve()).as_posix(),
        "start": start_line,
        "end": end_line,
        "text": snippet,
        "note": note.strip(),
    }


def is_denied(argv: list[str]) -> str | None:
    if not argv:
        return "empty command"
    base = Path(argv[0]).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    lowered = [part.lower() for part in argv]
    joined = " ".join(lowered)
    if base in {"format", "diskpart"}:
        return "refused destructive command"
    delete_flags = {"-r", "-rf", "-fr", "/s", "/q"}
    if base in {"rm", "del", "rmdir", "rd"} and any(
        part in delete_flags or part.startswith("-r") for part in lowered[1:]
    ):
        return "refused recursive delete"
    if "remove-item" in joined and "-recurse" in joined:
        return "refused recursive delete"
    if base == "git":
        if "push" in lowered:
            return "refused git push"
        if "reset" in lowered and "--hard" in lowered:
            return "refused git reset --hard"
        if "clean" in lowered and (
            "-fd" in lowered or "-df" in lowered or ("-f" in lowered and "-d" in lowered)
        ):
            return "refused git clean"
    return None


def run_cmd(root: Path, argv: list[str], timeout: int = RUN_TIMEOUT) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(root.resolve()),
            shell=False,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return 124, f"timed out after {timeout}s\n{stdout}\n{stderr}".strip()
    except OSError as exc:
        return 127, str(exc)
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    return completed.returncode, output


def same_argv(left: list[str], right: list[str]) -> bool:
    return [str(part) for part in left] == [str(part) for part in right]
