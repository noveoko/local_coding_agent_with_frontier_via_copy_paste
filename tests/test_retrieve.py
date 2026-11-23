import tempfile
import unittest
from pathlib import Path

from bbagent.retrieve import Index, chunk_python, tokenize


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class TokenizeTests(unittest.TestCase):
    def test_camel_and_snake_parts(self):
        camel = tokenize("getUserName")
        self.assertIn("getusername", camel)
        self.assertIn("user", camel)
        self.assertIn("name", camel)
        snake = tokenize("get_user_name")
        self.assertIn("get_user_name", snake)
        self.assertIn("user", snake)


class ChunkTests(unittest.TestCase):
    def test_methods_get_qualnames(self):
        text = "class Box:\n    def open(self):\n        return 1\n"
        chunks = chunk_python("box.py", text)
        names = {chunk.qualname: chunk.text for chunk in chunks}
        self.assertIn("Box", names)
        self.assertIn("Box.open", names)
        self.assertNotIn("return", names["Box"])


class RetrievalTests(unittest.TestCase):
    def test_embedding_links_cooccurrence_neighbors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "retry_sched.py", "def schedule_retry(job):\n    return job.schedule()\n")
            _write(root, "backoff_sched.py", "def schedule_backoff(job):\n    return job.schedule()\n")
            _write(root, "paint.py", "def render_canvas(color):\n    return color.hex()\n")
            _write(root, "invoice.py", "def total_invoice(amount):\n    return amount.cents()\n")
            _write(root, "garden.py", "def water_plants(volume):\n    return volume.liters()\n")
            _write(root, "music.py", "def play_melody(tempo):\n    return tempo.bpm()\n")
            index = Index(root)
            index.build()
            hits = index.embed_hits("retry", k=10)
            scores = {hit.path: hit.score for hit in hits}
            self.assertGreater(scores.get("backoff_sched.py", 0), scores.get("paint.py", 0))

    def test_exact_identifier_wins_bm25_fusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "names.py", 'def getUserName(user):\n    return user["name"]\n')
            _write(root, "titles.py", "def format_title(name):\n    label = name.strip()\n    return label\n")
            index = Index(root)
            index.build()
            index.save()
            again = Index(root)
            again.refresh()
            hits = again.search("getUserName", k=4)
            self.assertTrue(hits)
            self.assertEqual(hits[0].path, "names.py")
            self.assertEqual(hits[0].qualname, "getUserName")

    def test_gitignore_and_secrets_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, ".gitignore", "secret_dir/\n")
            _write(root, "secret_dir/hidden.py", "def hiddenmarker():\n    return 1\n")
            _write(root, ".env", "SUPERSECRETTOKEN=1\n")
            _write(root, "visible.py", "def visiblemarker():\n    return 1\n")
            index = Index(root)
            index.build()
            visible = index.search("visiblemarker")
            hidden = index.search("hiddenmarker")
            secret = index.search("SUPERSECRETTOKEN")
            self.assertTrue(any(hit.path == "visible.py" for hit in visible))
            self.assertFalse(any("hiddenmarker" in hit.text for hit in hidden))
            self.assertFalse(any("SUPERSECRETTOKEN" in hit.text for hit in secret))
