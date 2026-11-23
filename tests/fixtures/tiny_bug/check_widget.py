"""Unit test for the fixture. Named check_* so the agent suite does not collect it."""

import importlib.util
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).with_name("widget.py")
    spec = importlib.util.spec_from_file_location("tiny_widget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WidgetTest(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(_load().answer(), 2)


if __name__ == "__main__":
    unittest.main()
