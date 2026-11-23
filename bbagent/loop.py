"""Paste loop. Copilot chooses every action. This module only carries them out."""

from __future__ import annotations

from pathlib import Path

from bbagent import editing, protocol, retrieve, session, tools

WRITES = {"replace", "write"}


class Result:
    def __init__(
        self,
        ok: bool,
        summary: str,
        detail: str = "",
        wrote: bool = False,
        changed: list[str] | None = None,
    ) -> None:
        self.ok = ok
        self.summary = summary
        self.detail = detail
        self.wrote = wrote
        self.changed = changed or []


def run(root: Path, state: dict, replier, writer=print, confirmer=None, asker=None) -> int:
    root = Path(root).resolve()
    confirmer = confirmer or _default_confirm
    asker = asker or _default_ask
    state = session.attach_config(root, state)
    try:
        return _loop(root, state, replier, writer, confirmer, asker)
    except KeyboardInterrupt:
        session.save_session(root, state)
        writer("Session saved. Resume with: python -m bbagent --resume")
        return 130


def _loop(root, state, replier, writer, confirmer, asker) -> int:
    index = retrieve.Index(root, dim=int(state["config"]["embed_dim"]))
    index.refresh()
    writer(f"Indexed {len(index.chunks)} chunks from {index.file_count} files.")
    if not state.get("primed"):
        _prime(state, index)
        state["primed"] = True
        session.save_session(root, state)

    while state["turn"] < int(state["config"]["max_turns"]) and not state.get("finished"):
        state = session.attach_config(root, state)
        prompt = protocol.render_prompt(state, retrieve.repo_map(root))
        session.prompt_path(root).write_text(prompt, encoding="utf-8", newline="\n")
        session.save_session(root, state)
        reply = replier(prompt)
        if reply is None:
            session.save_session(root, state)
            writer("Session saved. Resume with: python -m bbagent --resume")
            return 130
        state["turn"] += 1
        state["gate"] = ""
        session.append_transcript(
            root, {"turn": state["turn"], "prompt": prompt, "reply": reply}
        )
        parsed = protocol.parse_reply(reply)
        if parsed.error:
            state["gate"] = parsed.error
            _record(state, [Result(False, "unparsed reply", parsed.error)])
            session.save_session(root, state)
            continue

        results: list[Result] = []
        wrote = False
        write_failed = False
        pending_done = False
        for action in parsed.actions:
            if action.kind == "done":
                pending_done = True
                state["done_summary"] = action.summary or action.body.strip()
                results.append(Result(True, "done requested", state["done_summary"]))
                continue
            result = _execute(action, root, state, index, writer, confirmer, asker)
            results.append(result)
            for rel in result.changed:
                if rel not in state["files_touched"]:
                    state["files_touched"].append(rel)
            wrote = wrote or result.wrote
            if not result.ok and action.kind in WRITES:
                write_failed = True
                state["gate"] = result.detail
                break
        if parsed.ignored_extra:
            extra = f"Ignored {parsed.ignored_extra} extra action blocks. The limit is three."
            results.append(Result(True, "ignored extra blocks", extra))

        test_command = state["config"]["test_command"]
        if wrote and test_command and not write_failed:
            _run_tests(root, state, test_command)
            if state["last_tests_passed"]:
                results.append(Result(True, "tests passed", state["last_test_output"]))
            else:
                signal = _signal(state["last_test_output"])
                state["gate"] = "Tests failed.\n" + state["last_test_output"]
                results.append(Result(False, f"tests failed {signal}", state["last_test_output"]))

        accepted = False
        if pending_done and not write_failed:
            accepted, note = _evaluate_done(root, state, confirmer)
            if accepted:
                state["finished"] = True
            else:
                state["gate"] = note
                results.append(Result(False, "done rejected", note))
        _record(state, results)
        session.save_session(root, state)
        if accepted:
            _announce(state, writer)
            return 0

    if state.get("finished"):
        return 0
    writer(
        f"Stopped after {state['turn']} turns. Session saved. "
        "Resume with: python -m bbagent --resume"
    )
    session.save_session(root, state)
    return 0


def _prime(state: dict, index: retrieve.Index) -> None:
    hits = index.search(state["task"], k=8)
    if not index.chunks:
        state["retrieval_note"] = "The workspace has no indexed text files."
        state["excerpts"] = []
        return
    if retrieve.confident(hits):
        state["excerpts"] = [retrieve.hit_dict(hit) for hit in hits]
        note = (
            "Top matches are ahead of the rest. "
            "You may edit immediately if this code is sufficient."
        )
    else:
        state["excerpts"] = []
        note = "Retrieval is uncertain. Use search or read before editing."
    if index.capped:
        note += " The index was capped at 4000 chunks."
    state["retrieval_note"] = note
    _mark_shown(state, state["excerpts"])


def _execute(action, root, state, index, writer, confirmer, asker) -> Result:
    kind = action.kind
    try:
        if kind == "plan":
            text = (action.body or action.summary).strip()
            if not text:
                return Result(False, "plan empty", "The plan block was empty.")
            state["plan"] = text
            return Result(True, "updated plan", text)
        if kind == "search":
            return _search(action, state, index)
        if kind == "read":
            return _read(action, root, state)
        if kind == "list":
            rel = action.path or "."
            return Result(True, f"list {rel}", tools.list_dir(root, rel))
        if kind == "replace":
            if not action.path:
                return Result(False, "replace missing path", "Provide path: ...")
            applied = editing.apply_replace(root, action.path, action.body, state["turn"])
            summary = f"replace {action.path}" + ("" if applied.ok else " failed")
            return Result(applied.ok, summary, applied.message, wrote=applied.ok, changed=applied.changed)
        if kind == "write":
            if not action.path:
                return Result(False, "write missing path", "Provide path: ...")
            applied = editing.apply_write(root, action.path, action.body, state["turn"])
            summary = f"write {action.path}" + ("" if applied.ok else " failed")
            return Result(applied.ok, summary, applied.message, wrote=applied.ok, changed=applied.changed)
        if kind == "run":
            return _run_action(action, root, state, confirmer)
        if kind == "ask":
            question = (action.body or action.summary).strip()
            if not question:
                return Result(False, "ask empty", "The ask block was empty.")
            writer(question)
            answer = asker(question)
            return Result(True, "ask", f"Q: {question}\nA: {answer}")
        return Result(
            False,
            f"unknown action {kind}",
            "Use plan, search, read, list, replace, write, run, ask, or done.",
        )
    except editing.PathError as exc:
        return Result(False, f"{kind} failed", str(exc))
    except OSError as exc:
        return Result(False, f"{kind} failed", str(exc))


def _search(action, state, index: retrieve.Index) -> Result:
    if index.stale():
        index.refresh()
    query = action.query.strip()
    if not query:
        return Result(False, "search missing query", "Provide query: ...")
    hits = index.search(query, k=8)
    state["excerpts"] = [retrieve.hit_dict(hit) for hit in hits]
    _mark_shown(state, state["excerpts"])
    listing = "\n".join(
        f"{item['path']}:{item['start']}-{item['end']} {item['qualname']}"
        for item in state["excerpts"]
    ) or "No chunks matched."
    return Result(True, f"search {query} -> {len(hits)} chunks", listing)


def _read(action, root, state) -> Result:
    if not action.path:
        return Result(False, "read missing path", "Provide path: ...")
    data = tools.read_range(root, action.path, action.start, action.end)
    if data.get("error"):
        return Result(False, f"read {action.path} failed", data["error"])
    item = {
        "path": data["path"],
        "start": data["start"],
        "end": data["end"],
        "qualname": "",
        "text": data["text"],
    }
    state["excerpts"].append(item)
    if len(state["excerpts"]) > 8:
        state["excerpts"] = state["excerpts"][-8:]
    _mark_shown(state, [item])
    detail = protocol.numbered(data["path"], data["start"], data["text"])
    if data.get("note"):
        detail += "\n" + data["note"]
    return Result(True, f"read {data['path']}:{data['start']}-{data['end']}", detail)


def _run_action(action, root, state, confirmer) -> Result:
    argv = list(action.argv)
    if not argv:
        return Result(False, "run missing argv", 'Provide argv as a JSON list, for example ["python", "-m", "unittest"].')
    denied = tools.is_denied(argv)
    if denied:
        return Result(False, "run denied", denied)
    test_command = state["config"]["test_command"]
    if not tools.same_argv(argv, test_command):
        shown = " ".join(argv)
        if not confirmer(f"Run {shown}? [y/n] "):
            return Result(False, "run refused by user", shown)
    code, output = tools.run_cmd(root, argv)
    output = protocol.head_tail(output, 60, 60)
    if tools.same_argv(argv, test_command):
        state["last_tests_passed"] = code == 0
        state["last_test_output"] = output
    return Result(code == 0, f"run {' '.join(argv)} -> exit {code}", output)


def _run_tests(root, state, command) -> None:
    code, output = tools.run_cmd(root, command)
    state["last_tests_passed"] = code == 0
    state["last_test_output"] = protocol.head_tail(output, 60, 60)


def _evaluate_done(root, state, confirmer) -> tuple[bool, str]:
    command = state["config"]["test_command"]
    summary = state.get("done_summary") or ""
    if command:
        if state.get("last_tests_passed") is None:
            _run_tests(root, state, command)
        if state.get("last_tests_passed"):
            return True, ""
        return False, "done was rejected because tests failed.\n" + (state.get("last_test_output") or "")
    message = "Copilot marked the task done.\n" + summary + "\nAccept this as done? [y/n] "
    if confirmer(message):
        return True, ""
    return False, "The user rejected done. Continue the task.\n" + summary


def _record(state: dict, results: list[Result]) -> None:
    summary = "; ".join(result.summary for result in results if result.summary)
    full = "\n\n".join(result.detail for result in results if result.detail)
    state["digest"].append({"turn": state["turn"], "summary": summary, "full": full})


def _mark_shown(state: dict, excerpts: list[dict]) -> None:
    for item in excerpts:
        key = f"{item['path']}:{item['start']}-{item['end']}"
        if key not in state["shown"]:
            state["shown"].append(key)


def _signal(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if "AssertionError" in stripped or stripped.startswith(("FAIL", "ERROR", "OK")):
            return stripped[:160]
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:160] if lines else "failed"


def _announce(state: dict, writer) -> None:
    files = ", ".join(state.get("files_touched") or []) or "nothing"
    lines = [f"Solved in {state['turn']} Copilot turns.", f"Changed: {files}"]
    if state["config"].get("test_command"):
        lines.append("Tests passed.")
    if state.get("done_summary"):
        lines.append(state["done_summary"])
    lines.append("Undo: python -m bbagent --undo")
    writer("\n".join(lines))


def _default_confirm(message: str) -> bool:
    try:
        answer = input(message).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _default_ask(question: str) -> str:
    del question
    try:
        return input("Your answer: ")
    except EOFError:
        return ""
