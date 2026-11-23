import unittest

from bbagent.protocol import parse_reply, render_prompt


REPLACE = """Sure, here is the edit.
```bbagent
action: replace
path: pkg/widget.py
```
<<<<<<< SEARCH
return None
=======
return value
>>>>>>> REPLACE
Thanks.
"""


class ParseTests(unittest.TestCase):
    def test_prose_around_a_replace_fence(self):
        parsed = parse_reply(REPLACE)
        self.assertEqual(parsed.error, "")
        self.assertEqual(len(parsed.actions), 1)
        action = parsed.actions[0]
        self.assertEqual(action.kind, "replace")
        self.assertEqual(action.path, "pkg/widget.py")
        self.assertIn("return None", action.body)
        self.assertIn("return value", action.body)

    def test_replace_markers_inside_the_fence(self):
        reply = """```bbagent
action: replace
path: pkg/widget.py
<<<<<<< SEARCH
return None
=======
return value
>>>>>>> REPLACE
```
"""
        parsed = parse_reply(reply)
        self.assertEqual(parsed.actions[0].kind, "replace")
        self.assertIn("return value", parsed.actions[0].body)

    def test_two_fences(self):
        reply = """I will look first.
```bbagent
action: plan
```
Fix the return value.

```bbagent
action: search
query: answer
```
"""
        parsed = parse_reply(reply)
        self.assertEqual([item.kind for item in parsed.actions], ["plan", "search"])
        self.assertIn("Fix the return", parsed.actions[0].body)
        self.assertEqual(parsed.actions[1].query, "answer")

    def test_missing_fence_asks_for_a_repair(self):
        parsed = parse_reply("I would change the file for you.")
        self.assertEqual(parsed.actions, [])
        self.assertIn("nothing was executed", parsed.error)
        self.assertIn("bbagent", parsed.error)

    def test_extra_fences_are_capped(self):
        blocks = "\n".join(
            f"```bbagent\naction: plan\n```\nplan {index}\n" for index in range(4)
        )
        parsed = parse_reply(blocks)
        self.assertEqual(len(parsed.actions), 3)
        self.assertEqual(parsed.ignored_extra, 1)


class RenderTests(unittest.TestCase):
    def test_prompt_contains_the_contract_and_task(self):
        state = _state("make answer return 2")
        text = render_prompt(state, "widget.py")
        self.assertIn("make answer return 2", text)
        self.assertIn("```bbagent", text)
        self.assertIn("<<<<<<< SEARCH", text)
        self.assertIn("Begin your reply with ```bbagent.", text)

    def test_budget_trims_excerpts(self):
        state = _state("do the thing")
        state["config"]["prompt_budget"] = 4000
        state["excerpts"] = [
            {"path": "a.py", "start": 1, "qualname": "", "text": "x" * 3000} for _ in range(6)
        ]
        text = render_prompt(state, "a.py\n" * 80)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("do the thing", text)

    def test_middle_history_collapses(self):
        state = _state("task")
        state["digest"] = [
            {"turn": 1, "summary": "first", "full": "FULL1"},
            {"turn": 2, "summary": "middle", "full": "FULL2 should hide"},
            {"turn": 3, "summary": "late", "full": "FULL3"},
            {"turn": 4, "summary": "last", "full": "FULL4"},
        ]
        text = render_prompt(state, "tree")
        self.assertIn("FULL1", text)
        self.assertNotIn("FULL2 should hide", text)
        self.assertIn("turn 2: middle", text)
        self.assertIn("FULL3", text)
        self.assertIn("FULL4", text)


def _state(task: str) -> dict:
    return {
        "task": task,
        "plan": "",
        "shown": [],
        "retrieval_note": "",
        "gate": "",
        "digest": [],
        "excerpts": [],
        "config": {"prompt_budget": 12000},
    }


if __name__ == "__main__":
    unittest.main()
