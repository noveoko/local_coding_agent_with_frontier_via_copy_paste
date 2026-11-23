"""Path jail, search/replace, snapshots, and undo."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from bbagent.session import DIR_NAME

RESERVED = frozenset({".git", DIR_NAME})
FUZZY_APPLY = 0.92
FUZZY_SHOW = 0.80
FUZZY_GAP = 0.03
WRITE_LIMIT = 40


class PathError(ValueError):
    pass


@dataclass
class ApplyResult:
    ok: bool
    message: str
    changed: list[str] = field(default_factory=list)
    fuzzy: bool = False


def resolve_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    raw = (rel or "").strip().replace("\\", "/")
    if not raw or raw == ".":
        raise PathError("missing path")
    if raw.startswith("@"):
        raise PathError("context sources are read-only")
    candidate = Path(raw)
    if candidate.is_absolute() or raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise PathError("path escapes the workspace")
    resolved = (root / raw).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise PathError("path escapes the workspace") from None
    if relative.parts and relative.parts[0] in RESERVED:
        raise PathError("path is reserved")
    return resolved


def snapshot_dir(root: Path, turn: int) -> Path:
    return root.resolve() / DIR_NAME / "snapshots" / str(turn)


def remember_original(root: Path, turn: int, rel: str) -> None:
    """Copy the file as it is now, once per path per turn."""
    path = resolve_path(root, rel)
    directory = snapshot_dir(root, turn)
    manifest_path = directory / "manifest.json"
    manifest = {"files": {}}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = path.relative_to(root.resolve()).as_posix()
    if key in manifest["files"]:
        return
    entry: dict = {"existed": path.is_file()}
    if path.is_file():
        dest = directory / "files" / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    directory.mkdir(parents=True, exist_ok=True)
    manifest["files"][key] = entry
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def apply_replace(root: Path, rel: str, body: str, turn: int) -> ApplyResult:
    try:
        path = resolve_path(root, rel)
    except PathError as exc:
        return ApplyResult(False, str(exc))
    if not path.is_file():
        return ApplyResult(False, f"{rel} does not exist. Use write to create it.")
    pairs = extract_pairs(body)
    if not pairs:
        return ApplyResult(
            False,
            "No SEARCH/REPLACE block found. Use <<<<<<< SEARCH, =======, and >>>>>>> REPLACE.",
        )
    original = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines, trailing = _split(original)
    notes: list[str] = []
    fuzzy = False
    for pair_index, (search, replacement) in enumerate(pairs, start=1):
        search_lines = search.splitlines()
        if not any(line.strip() for line in search_lines):
            return ApplyResult(False, f"Pair {pair_index} has an empty SEARCH block.")
        matched = match_search(lines, search_lines)
        if matched.status == "ambiguous":
            return ApplyResult(False, f"Pair {pair_index} matched {matched.count} places in {rel}. Add surrounding lines.")
        if matched.status in {"close", "miss"}:
            return ApplyResult(False, f"Pair {pair_index} did not match {rel}.\n{matched.excerpt}")
        replace_lines = replacement.splitlines()
        lines = lines[: matched.start] + replace_lines + lines[matched.end :]
        if matched.status == "fuzzy":
            fuzzy = True
            notes.append(
                f"Pair {pair_index} applied with a fuzzy match at lines {matched.start + 1}-{matched.end}."
            )
        elif matched.status == "stripped":
            notes.append(f"Pair {pair_index} matched after stripping trailing whitespace.")
        else:
            notes.append(f"Pair {pair_index} replaced lines {matched.start + 1}-{matched.end}.")
    updated = _join(lines, trailing)
    key = path.relative_to(root.resolve()).as_posix()
    remember_original(root, turn, key)
    path.write_text(updated, encoding="utf-8", newline="\n")
    message = f"Updated {key}. " + " ".join(notes)
    return ApplyResult(True, message.strip(), [key], fuzzy=fuzzy)


def apply_write(root: Path, rel: str, body: str, turn: int) -> ApplyResult:
    try:
        path = resolve_path(root, rel)
    except PathError as exc:
        return ApplyResult(False, str(exc))
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\n"):
        text = text[1:]
    if not text.endswith("\n"):
        text += "\n"
    if path.is_file():
        current = path.read_text(encoding="utf-8", errors="replace")
        existing_lines = current.splitlines()
        if len(existing_lines) > WRITE_LIMIT:
            preview = "\n".join(existing_lines[:40])
            return ApplyResult(
                False,
                f"{rel} has {len(existing_lines)} lines. Refusing a full overwrite. Use replace.\n{preview}",
            )
    elif path.exists():
        return ApplyResult(False, f"{rel} exists and is not a file.")
    key = path.relative_to(root.resolve()).as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)
    remember_original(root, turn, key)
    path.write_text(text, encoding="utf-8", newline="\n")
    return ApplyResult(True, f"Wrote {key} ({len(text.splitlines())} lines).", [key])


def undo(root: Path) -> str:
    base = root.resolve() / DIR_NAME / "snapshots"
    if not base.is_dir():
        return "Nothing to undo."
    turns = sorted((int(path.name) for path in base.iterdir() if path.is_dir() and path.name.isdigit()))
    if not turns:
        return "Nothing to undo."
    turn = turns[-1]
    directory = base / str(turn)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for rel, info in manifest.get("files", {}).items():
        dest = resolve_path(root, rel)
        if info.get("existed"):
            src = directory / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        elif dest.is_file():
            dest.unlink()
    shutil.rmtree(directory)
    return f"Restored snapshot {turn}."


def extract_pairs(body: str) -> list[tuple[str, str]]:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() == "<<<<<<< SEARCH":
            index += 1
            search: list[str] = []
            while index < len(lines) and lines[index].strip() != "=======":
                search.append(lines[index])
                index += 1
            if index >= len(lines):
                break
            index += 1
            replacement: list[str] = []
            while index < len(lines) and lines[index].strip() != ">>>>>>> REPLACE":
                replacement.append(lines[index])
                index += 1
            pairs.append(("\n".join(search), "\n".join(replacement)))
        index += 1
    return pairs


@dataclass
class Match:
    status: str
    start: int = 0
    end: int = 0
    count: int = 0
    excerpt: str = ""
    ratio: float = 0.0


def match_search(file_lines: list[str], search_lines: list[str]) -> Match:
    exact = _line_spans(file_lines, search_lines, strip=False)
    if len(exact) == 1:
        start = exact[0]
        return Match("exact", start, start + len(search_lines), 1)
    if len(exact) > 1:
        return Match("ambiguous", count=len(exact))
    stripped = _line_spans(file_lines, search_lines, strip=True)
    if len(stripped) == 1:
        start = stripped[0]
        return Match("stripped", start, start + len(search_lines), 1)
    if len(stripped) > 1:
        return Match("ambiguous", count=len(stripped))
    windows = _windows(file_lines, search_lines)
    if not windows:
        return Match("miss", excerpt=_miss_excerpt(file_lines, search_lines))
    windows.sort(key=lambda item: item[0], reverse=True)
    best_ratio, best_start, best_end = windows[0]
    second = windows[1][0] if len(windows) > 1 else 0.0
    excerpt = _number_span(file_lines, best_start, best_end)
    if best_ratio >= FUZZY_APPLY and best_ratio - second >= FUZZY_GAP:
        return Match("fuzzy", best_start, best_end, 1, ratio=best_ratio)
    if best_ratio >= FUZZY_SHOW:
        return Match("close", excerpt=excerpt, ratio=best_ratio)
    return Match("miss", excerpt=_miss_excerpt(file_lines, search_lines), ratio=best_ratio)


def _line_spans(file_lines: list[str], search_lines: list[str], strip: bool) -> list[int]:
    if not search_lines:
        return []
    hay = [line.rstrip() for line in file_lines] if strip else list(file_lines)
    needle = [line.rstrip() for line in search_lines] if strip else list(search_lines)
    width = len(needle)
    found = []
    limit = len(hay) - width + 1
    for index in range(max(limit, 0)):
        if hay[index : index + width] == needle:
            found.append(index)
    return found


def _windows(file_lines: list[str], search_lines: list[str]) -> list[tuple[float, int, int]]:
    width = len(search_lines)
    if width == 0 or not file_lines:
        return []
    target = "\n".join(line.rstrip() for line in search_lines)
    scored = []
    if len(file_lines) <= width:
        ratio = SequenceMatcher(None, "\n".join(line.rstrip() for line in file_lines), target).ratio()
        return [(ratio, 0, len(file_lines))]
    for index in range(len(file_lines) - width + 1):
        window = "\n".join(line.rstrip() for line in file_lines[index : index + width])
        ratio = SequenceMatcher(None, window, target).ratio()
        scored.append((ratio, index, index + width))
    return scored


def _number_span(file_lines: list[str], start: int, end: int) -> str:
    rows = []
    for number in range(start, end):
        rows.append(f"{number + 1}| {file_lines[number]}")
    return "\n".join(rows)


def _miss_excerpt(file_lines: list[str], search_lines: list[str]) -> str:
    distinctive = ""
    for line in search_lines:
        if any(char.isalpha() for char in line):
            distinctive = line.rstrip()
            break
    if distinctive:
        hits = [index for index, line in enumerate(file_lines) if line.rstrip() == distinctive]
        if len(hits) == 1:
            center = hits[0]
            start = max(0, center - 6)
            end = min(len(file_lines), center + 7)
            return "Closest region:\n" + _number_span(file_lines, start, end)
    preview = _number_span(file_lines, 0, min(len(file_lines), 40))
    return "No close match. File starts:\n" + preview


def _split(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], True
    trailing = text.endswith("\n")
    return text.splitlines(), trailing


def _join(lines: list[str], trailing: bool) -> str:
    body = "\n".join(lines)
    if trailing:
        body += "\n"
    return body
