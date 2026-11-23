"""Prompt template and the bbagent fence parser.

Copilot adds prose. Only fenced blocks are executed. Each prompt repeats the
whole state, because the next paste may land in a fresh chat.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

EXAMPLE = """```bbagent
action: replace
path: pkg/widget.py
```
<<<<<<< SEARCH
return None
=======
return value
>>>>>>> REPLACE"""

HEADER = """You are the reasoning engine for a local coding agent. You cannot see the filesystem. A program on the user's machine executes the actions you specify and pastes the results back. This message is the entire state. Ignore any earlier chat and any Microsoft 365 tenant files. The excerpts below are the only source of truth.

Reply with one to three fenced blocks tagged bbagent. Begin your reply with ```bbagent. Prose outside the fences is ignored. Prefer the smallest edit. Use replace for edits. Use write only for a new file or a file of 40 lines or fewer. Do not invent file contents. If you have not seen a file, read or search it first. When the task is solved, emit done.

Example:
""" + EXAMPLE + """

Actions: plan, search, read, list, replace, write, run, ask, done.
- plan: the plan text after the fence
- search: a query field
- read: path, and optional start and end line numbers
- list: optional path
- replace: path, then one or more SEARCH / REPLACE blocks
- write: path, then the full file body
- run: argv as a JSON list
- ask: a question for the user
- done: a short summary
"""

FENCE_RE = re.compile(r"```bbagent[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL)
HEADER_RE = re.compile(r"^[A-Za-z_]+\s*:")


@dataclass
class Action:
    kind: str
    path: str = ""
    query: str = ""
    start: int | None = None
    end: int | None = None
    body: str = ""
    argv: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ParseResult:
    actions: list[Action]
    error: str = ""
    ignored_extra: int = 0


def repair_message() -> str:
    return (
        "Your previous reply had no ```bbagent fence, so nothing was executed. "
        "Begin your reply with ```bbagent and include one to three action blocks.\n"
        "Example:\n" + EXAMPLE
    )


def numbered(path: str, start: int, text: str, qualname: str = "") -> str:
    lines = text.splitlines() or [""]
    end = start + len(lines) - 1
    width = max(1, len(str(max(end, start))))
    label = f"{path}:{start}-{end}"
    if qualname:
        label += f"  {qualname}"
    body = [f"{number:>{width}}| {line}" for number, line in enumerate(lines, start)]
    return label + "\n" + "\n".join(body)


def head_tail(text: str, head: int = 40, tail: int = 40) -> str:
    lines = text.splitlines()
    if len(lines) <= head + tail + 1:
        return text
    omitted = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"... {omitted} lines omitted ..."] + lines[-tail:])


def parse_reply(reply: str) -> ParseResult:
    text = reply.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(FENCE_RE.finditer(text))
    if not matches:
        return ParseResult(actions=[], error=repair_message())
    ignored = max(0, len(matches) - 3)
    actions: list[Action] = []
    for index, match in enumerate(matches[:3]):
        after_start = match.end()
        after_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        actions.append(_parse_one(match.group(1), text[after_start:after_end]))
    return ParseResult(actions=actions, ignored_extra=ignored)


def _parse_one(inner: str, after: str) -> Action:
    meta, inner_body = _header_and_body(inner)
    kind = meta.get("action", "").strip().lower()
    body = _strip_wrapping_fence((inner_body + "\n" + after).strip())
    action = Action(kind=kind or "unknown", body=body)
    action.path = meta.get("path", "").strip()
    action.query = meta.get("query", "").strip()
    action.summary = meta.get("summary", "").strip()
    action.start = _optional_int(meta.get("start"))
    action.end = _optional_int(meta.get("end"))
    if "argv" in meta:
        action.argv = _parse_argv(meta["argv"])
    elif kind == "run":
        action.argv = _parse_argv(body)
    if kind == "search" and not action.query:
        action.query = body.strip()
    if kind == "done" and not action.summary:
        action.summary = body.strip()
    if kind == "list" and not action.path:
        action.path = meta.get("path", ".") or "."
    return action


def _header_and_body(inner: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    lines = inner.strip("\n").splitlines()
    body_at = 0
    for index, line in enumerate(lines):
        if line.strip() == "":
            body_at = index + 1
            break
        if HEADER_RE.match(line) and not line.lstrip().startswith("<<<<<<<"):
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
            body_at = index + 1
            continue
        body_at = index
        break
    return meta, "\n".join(lines[body_at:])


def _strip_wrapping_fence(body: str) -> str:
    stripped = body.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return stripped


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_argv(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return data
        return []
    # A single JSON list may be wrapped in prose. Take the first array.
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return _parse_argv(text[start : end + 1])
    return []


def _full_turn_ids(digest: list[dict]) -> set[int]:
    if not digest:
        return set()
    turns = [int(item["turn"]) for item in digest]
    keep = {turns[0], turns[-1]}
    if len(turns) >= 2:
        keep.add(turns[-2])
    return keep


def render_prompt(state: dict, tree: str) -> str:
    budget = int(state.get("config", {}).get("prompt_budget", 12000))
    excerpts = [dict(item) for item in state.get("excerpts") or []]
    digest = [dict(item) for item in state.get("digest") or []]
    tree_text = tree
    shrunk_excerpt = False
    shrunk_digest = False
    shrunk_tree = False
    while True:
        text = _compose(state, tree_text, excerpts, digest)
        if len(text) <= budget:
            return text
        if len(excerpts) > 1:
            excerpts.pop()
            continue
        if excerpts and not shrunk_excerpt:
            excerpts[0]["text"] = head_tail(str(excerpts[0].get("text", "")), 30, 20)
            shrunk_excerpt = True
            continue
        if digest and not shrunk_digest:
            for item in digest:
                item["full"] = head_tail(str(item.get("full", "")), 20, 20)
            shrunk_digest = True
            continue
        if not shrunk_tree and len(tree_text) > 400:
            tree_text = "\n".join(tree_text.splitlines()[:30]) + "\n..."
            shrunk_tree = True
            continue
        if len(text) > budget:
            return text[: budget - 16] + "\n...[truncated]"
        return text


def _compose(state: dict, tree: str, excerpts: list[dict], digest: list[dict]) -> str:
    parts = [HEADER.rstrip(), "", "TASK", state.get("task", "").rstrip(), "", "PLAN"]
    plan = (state.get("plan") or "").strip()
    parts.append(plan if plan else "No plan yet. Your first block may be a plan.")
    parts.extend(["", "REPO MAP", tree.strip() or "(empty)"])
    shown = state.get("shown") or []
    if shown:
        visible = shown[-80:]
        parts.extend(["", "ALREADY SHOWN", "\n".join(visible)])
    note = (state.get("retrieval_note") or "").strip()
    if note:
        parts.extend(["", "RETRIEVAL", note])
    if excerpts:
        blocks = []
        for item in excerpts:
            blocks.append(
                numbered(
                    item.get("path", ""),
                    int(item.get("start", 1)),
                    item.get("text", ""),
                    item.get("qualname", ""),
                )
            )
        parts.extend(["", "EXCERPTS", "\n\n".join(blocks)])
    if digest:
        parts.extend(["", "HISTORY", _history(digest)])
    gate = (state.get("gate") or "").strip()
    if gate:
        parts.extend(["", "GATE", gate])
    parts.extend(["", "Begin your reply with ```bbagent."])
    return "\n".join(parts).rstrip() + "\n"


def _history(digest: list[dict]) -> str:
    full_ids = _full_turn_ids(digest)
    blocks = []
    for item in digest:
        turn = int(item["turn"])
        summary = (item.get("summary") or "").strip()
        if turn in full_ids and (item.get("full") or "").strip():
            blocks.append(f"turn {turn}: {summary}\n{item['full'].rstrip()}")
        else:
            blocks.append(f"turn {turn}: {summary}")
    return "\n\n".join(blocks)
