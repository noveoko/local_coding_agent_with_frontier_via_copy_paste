import tempfile
import unittest
from pathlib import Path

from bbagent.retrieve import Corpus, ContextError, Index, build_sources, chunk_python, tokenize


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


class ContextDirTests(unittest.TestCase):
    def test_missing_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ContextError):
                build_sources(root, [str(root / "no-such-dir")])

    def test_corpus_search_prefixes_external_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "ws"
            extra = base / "spec-pack"
            workspace.mkdir()
            extra.mkdir()
            _write(workspace, "app.py", "def app_main():\n    return 1\n")
            _write(extra, "oracle.py", "def unique_oracle_marker():\n    return 1\n")
            sources = build_sources(workspace, [str(extra)])
            corpus = Corpus(workspace, sources, dim=128)
            hits = corpus.search("unique_oracle_marker", k=8)
            self.assertTrue(hits)
            self.assertTrue(any(hit.path.startswith("@") and hit.path.endswith("oracle.py") for hit in hits))
            self.assertTrue(any("unique_oracle_marker" in hit.text for hit in hits))
            extras = [source for source in sources if source.alias]
            self.assertEqual(len(extras), 1)
            self.assertFalse(extras[0].writable)
            self.assertEqual(extras[0].prefix, f"@{extras[0].alias}")
            from bbagent.tools import list_dir, read_range

            listed = list_dir(workspace, extras[0].prefix, sources)
            self.assertIn("oracle.py", listed)
            data = read_range(workspace, f"{extras[0].prefix}/oracle.py", None, None, sources)
            self.assertNotIn("error", data)
            self.assertIn("unique_oracle_marker", data["text"])
            self.assertTrue(data["path"].startswith("@"))

    def test_inside_workspace_dir_keeps_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "app.py", "def app_main():\n    return 1\n")
            _write(root, "docs/guide.md", "UniqueInsideToken lives here.\n")
            sources = build_sources(root, ["docs"])
            corpus = Corpus(root, sources, dim=128)
            extras = [source for source in sources if source.alias]
            self.assertTrue(extras[0].inside)
            self.assertTrue(extras[0].writable)
            self.assertEqual(extras[0].prefix, "docs")
            hits = corpus.search("UniqueInsideToken", k=8)
            self.assertTrue(any(hit.path == "docs/guide.md" for hit in hits))
            self.assertFalse(any(hit.path.startswith("@") for hit in hits))
