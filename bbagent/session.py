"""Session, config, and transcript on disk under .bbagent/."""

from __future__ import annotations

import json
from pathlib import Path

DIR_NAME = ".bbagent"

DEFAULT_CONFIG = {
    "test_command": [],
    "prompt_budget": 12000,
    "max_turns": 25,
    "embed_dim": 128,
    "context_dirs": [],
}


def agent_dir(root: Path) -> Path:
    return root / DIR_NAME


def config_path(root: Path) -> Path:
    return agent_dir(root) / "config.json"


def session_path(root: Path) -> Path:
    return agent_dir(root) / "session.json"


def transcript_path(root: Path) -> Path:
    return agent_dir(root) / "transcript.jsonl"


def prompt_path(root: Path) -> Path:
    return agent_dir(root) / "prompt.md"


def reply_path(root: Path) -> Path:
    return agent_dir(root) / "reply.md"


def load_config(root: Path) -> dict:
    path = config_path(root)
    if not path.exists():
        agent_dir(root).mkdir(parents=True, exist_ok=True)
        save_config(root, dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    if not isinstance(merged["test_command"], list):
        raise ValueError("test_command must be a JSON list of strings")
    merged["test_command"] = [str(part) for part in merged["test_command"]]
    merged["prompt_budget"] = int(merged["prompt_budget"])
    merged["max_turns"] = int(merged["max_turns"])
    merged["embed_dim"] = int(merged["embed_dim"])
    dirs = merged.get("context_dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(item, str) for item in dirs):
        raise ValueError("context_dirs must be a JSON list of strings")
    merged["context_dirs"] = [str(item) for item in dirs]
    return merged


def save_config(root: Path, config: dict) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")


def merge_dirs(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
    return out


def new_session(root: Path, task: str, context_dirs: list[str] | None = None) -> dict:
    config = load_config(root)
    session = {
        "version": 1,
        "task": task,
        "turn": 0,
        "plan": "",
        "digest": [],
        "shown": [],
        "gate": "",
        "excerpts": [],
        "retrieval_note": "",
        "files_touched": [],
        "last_tests_passed": None,
        "last_test_output": "",
        "finished": False,
        "done_summary": "",
        "primed": False,
        "context_dirs": merge_dirs(config.get("context_dirs") or [], context_dirs or []),
        "context_sources": [],
        "context_maps": {},
    }
    save_session(root, session)
    return session


def load_session(root: Path) -> dict | None:
    path = session_path(root)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(root: Path, session: dict) -> None:
    path = session_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {key: value for key, value in session.items() if key != "config"}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def append_transcript(root: Path, record: dict) -> None:
    path = transcript_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def attach_config(root: Path, session: dict) -> dict:
    session["config"] = load_config(root)
    return session
