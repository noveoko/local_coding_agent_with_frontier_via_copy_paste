# bbagent

A coding agent whose only language model is Microsoft 365 Copilot. You paste a prompt in, copy the reply out, and press Enter. The local program never calls a model and never opens a network connection. It is Python 3.11 or newer and the standard library only.

Copilot decides what to do. The program retrieves code, applies edits, runs tests, and builds the next prompt.

## Paste ritual

From the repository you want to change:

```text
python -m bbagent "describe the task"
```

If this checkout is not on `PYTHONPATH`, point at it and pass the workspace:

```text
python D:\Projects\black_box_llm_agent\bbagent\__main__.py --root D:\path\to\repo "describe the task"
```

Each turn:

1. The prompt is copied to the clipboard and written to `.bbagent/prompt.md`.
2. Paste it into Microsoft 365 Copilot. A fresh chat is fine. Every prompt carries the whole state.
3. Copy the entire reply.
4. Press Enter in the terminal. If the clipboard is still the prompt, the program says so and waits. You can also save the reply as `.bbagent/reply.md`.

The loop stops when Copilot emits `done` and the checks below pass, when you press Ctrl+C, or after 25 turns.

```text
python -m bbagent --resume
python -m bbagent --status
python -m bbagent --undo
python -m bbagent --context D:\Projects\api-spec --context D:\Projects\docs "describe the task"
```

`--undo` restores the newest snapshot of files that turn changed.

## What Copilot must reply with

Prose is ignored. The program executes one to three `bbagent` fences, in order. A failed edit stops the rest of that batch. If Copilot's copy button drops the fence, a line that starts with `action:` plus a SEARCH/REPLACE body is still executed.

````
```bbagent
action: replace
path: pkg/widget.py
<<<<<<< SEARCH
exact lines currently in the file
=======
replacement lines
>>>>>>> REPLACE
```
````

| Action | What the program does |
| --- | --- |
| `plan` | Stores the plan and repeats it later. |
| `search` | Ranks chunks and puts the top ones in the next prompt. |
| `read` | Returns a line-numbered excerpt. |
| `list` | Lists one directory. |
| `replace` | Applies a unique search/replace. Trailing whitespace is ignored. A single close match above 0.92 is applied and called out. |
| `write` | Creates a file, or overwrites one of 40 lines or fewer. |
| `run` | Runs an argv list with `shell=False` and a 60 second timeout. |
| `ask` | Prints a question and sends your answer back. |
| `done` | Stops when the gate below passes. |

## When a task is solved

Set `test_command` in `.bbagent/config.json` to an argv list. After a successful edit the program runs it. `done` is accepted only when that command exits 0. A `done` while tests fail is sent back to Copilot with the output.

```json
{
  "test_command": ["python", "-m", "unittest", "discover"],
  "prompt_budget": 12000,
  "max_turns": 25,
  "embed_dim": 128,
  "context_dirs": ["D:/Projects/api-spec", "../shared-docs"]
}
```

Unittest is in the standard library. The program does not assume pytest is installed. With an empty `test_command`, `done` asks you to accept the summary.

## Retrieval

Before the first prompt, and on every `search`, the workspace is ranked with two stdlib methods:

- BM25 over identifiers, including camelCase and snake_case pieces.
- Reflective random indexing. Each token gets a deterministic ±1 vector from `sha256`. A token's stored vector is the sum of the tokens that share a chunk with it in this repo, so names that sit together rank near each other. There is no downloaded model.

Reciprocal rank fusion merges the lists. At most 8 chunks, and at most 3 from one file, go into the prompt. The budget is 12,000 characters. Secrets (`.env`, `*.pem`, private key names) are left out of excerpts. `.gitignore` names, `.git`, and `.bbagent` are skipped.

## Context directories

Pass related trees with `--context` (repeatable) or `context_dirs` in `.bbagent/config.json`. Relative paths are from the workspace. `--resume` keeps the list from the session.

Each extra directory is indexed on its own. Hits from outside the workspace show up as `@alias/path` (`alias` is the folder name). The first prompt lists those sources, attaches a compact map of each, and retrieves chunks that match the task. `read` and `list` accept `@alias/path`. `replace` and `write` do not. Indexes for foreign trees live under `.bbagent/index/context/` in the workspace.

A directory inside the workspace stays writable through its normal relative path. Naming it in `context_dirs` adds a map and a retrieval pass over that subtree.

## Safety

Edits stay inside the workspace. `.git` and `.bbagent` cannot be written. The configured test command runs on its own. Any other `run` asks for a `y` first. Recursive deletes, disk formats, `git push`, `git reset --hard`, and `git clean -fd` are refused. Snapshots live in `.bbagent/snapshots/`.

## Tests

```text
python -m unittest discover -s tests -p "test_*.py"
```

The suite feeds canned Copilot replies through the same parser and loop. It does not need a Copilot login.

To drive the real paste loop yourself (temp copy of the tiny_bug fixture, Microsoft 365 Copilot):

```text
python tests/test_hitl.py
```

`unittest discover` skips that test unless `BBAGENT_HITL` is `1`.
