import unittest

from bbagent import clipboard


class ClipboardTests(unittest.TestCase):
    def test_roundtrip_when_available(self):
        if not clipboard.available():
            self.skipTest("clipboard only exists on Windows")
        previous = clipboard.get_text()
        token = "bbagent-clipboard-check"
        try:
            if not clipboard.set_text(token):
                self.skipTest("clipboard is locked")
            self.assertEqual(clipboard.get_text(), token)
        finally:
            if previous is not None:
                clipboard.set_text(previous)


if __name__ == "__main__":
    unittest.main()
