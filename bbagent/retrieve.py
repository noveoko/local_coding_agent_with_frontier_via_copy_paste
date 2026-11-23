"""Chunk the workspace, then rank with BM25 and reflective random indexing.

Both ranks use only the standard library. The embedding is a hash-seeded
random index: a token's vector is the sum of the base vectors of the other
tokens that share a chunk with it. No model and no network.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

K1 = 1.2
B = 0.75
RRF_K = 60
MAX_CHUNKS = 4000
MAX_FILE_BYTES = 200_000
EMBED_NONZEROS = 6
SIF_A = 1e-3
# Structural tokens stay in BM25. They are left out of the reflective sum so
# they do not pull every chunk toward the same direction. Tokens that occur in
# more than half the chunks are dropped the same way once the repo is large
# enough for that ratio to mean something (four or more chunks).
EMBED_STOP = frozenset(
    """
    def class return self none true false import from if else elif for while
    not and or pass raise with as try except finally lambda yield async await
    global nonlocal assert break continue del is
    """.split()
)
ENGLISH_STOP = frozenset(
    "the a an of to and or for in on with from as is at by be it this that".split()
)
SKIP_DIRS = frozenset(
    {".git", ".bbagent", "node_modules", "venv", ".venv", "__pycache__", ".hg", ".svn"}
)
BINARY_SUFFIXES = frozenset(
    """
    .png .jpg .jpeg .gif .webp .ico .pdf .zip .gz .woff .woff2 .exe .dll
    .pyd .pyc .so .dylib .bin .mp4 .mp3 .wav .ogg .7z .tar .bz2 .xz .jar
    .class .o .obj .wasm .ttf .eot
    """.split()
)
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
HEADING_RE = re.compile(r"^#{1,6} ")
PART_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    qualname: str
    text: str


@dataclass
class Hit:
    path: str
    start: int
    end: int
    qualname: str
    text: str
    score: float


@dataclass(frozen=True)
class Source:
    alias: str
    root: Path
    writable: bool
    prefix: str
    inside: bool

    def prefixed(self, rel: str) -> str:
        text = (rel or "").replace("\\", "/").strip("/")
        if not self.prefix:
            return text or "."
        if not text or text == ".":
            return self.prefix
        return f"{self.prefix}/{text}"


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for match in TOKEN_RE.findall(text):
        if match.isdigit():
            out.append(match)
            continue
        lower = match.lower()
        if lower not in ENGLISH_STOP:
            out.append(lower)
        pieces = PART_RE.findall(match)
        if "_" in match:
            snake = [part for part in match.split("_") if part]
            if len(snake) > 1:
                pieces = snake
        if len(pieces) > 1:
            for piece in pieces:
                folded = piece.lower()
                if folded and folded not in ENGLISH_STOP and folded != lower:
                    out.append(folded)
    return out


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    if path.suffix.lower() in {".pem", ".key"}:
        return True
    return False


def parse_gitignore(text: str) -> list[str]:
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        patterns.append(stripped)
    return patterns


def ignored(rel: str, patterns: list[str]) -> bool:
    """A small .gitignore subset: comments and negations are skipped.

    A pattern with no slash matches any path component. A pattern with a
    slash matches the relative path.
    """
    parts = rel.split("/")
    for raw in patterns:
        pattern = raw.strip("/")
        if not pattern:
            continue
        if "/" in raw.strip():
            if fnmatch_path(rel, pattern) or rel == pattern or rel.startswith(pattern + "/"):
                return True
            continue
        if any(fnmatch_path(part, pattern) for part in parts):
            return True
    return False


def fnmatch_path(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)


def load_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.is_file():
        return []
    try:
        return parse_gitignore(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def iter_source_files(root: Path) -> list[Path]:
    root = root.resolve()
    patterns = load_patterns(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            entry = current / name
            if name in SKIP_DIRS or entry.is_symlink():
                continue
            try:
                rel = entry.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if ignored(rel, patterns):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = current / name
            if path.is_symlink() or path.suffix.lower() in BINARY_SUFFIXES:
                continue
            if is_secret(path):
                continue
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if ignored(rel, patterns):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(path)
    found.sort(key=lambda item: item.as_posix().lower())
    return found


def looks_binary(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return True
    return b"\x00" in sample


def read_text(path: Path) -> str | None:
    if looks_binary(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return None


def chunk_file(rel: str, text: str) -> list[Chunk]:
    if rel.endswith(".py"):
        parts = chunk_python(rel, text)
        if parts:
            return parts
    return chunk_text(rel, text)


def chunk_python(rel: str, text: str) -> list[Chunk]:
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    chunks: list[Chunk] = []
    occupied: set[int] = set()

    def mark(start: int, end: int) -> None:
        for number in range(start, end + 1):
            occupied.add(number)

    def span(node: ast.AST) -> tuple[int, int]:
        start = getattr(node, "lineno", 1) or 1
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = decorators[0].lineno
        end = getattr(node, "end_lineno", None) or start
        return start, end

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            start, end = span(node)
            mark(start, end)
            sig_end = node.lineno
            if node.body and _is_docstring(node.body[0]):
                sig_end = min(node.body[0].end_lineno or sig_end, start + 39)
            sig_end = max(sig_end, node.lineno)
            qual = ".".join(self.stack + [node.name])
            body = "\n".join(lines[start - 1 : sig_end])
            chunks.append(Chunk(rel, start, sig_end, qual, body))
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._function(node)

        def _function(self, node: ast.AST) -> None:
            start, end = span(node)
            mark(start, end)
            name = getattr(node, "name", "fn")
            qual = ".".join(self.stack + [name])
            body = "\n".join(lines[start - 1 : end])
            chunks.append(Chunk(rel, start, end, qual, body))
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

    Visitor().visit(tree)
    chunks.extend(_leftover(rel, lines, occupied))
    return [chunk for chunk in chunks if chunk.text.strip()]


def _is_docstring(node: object) -> bool:
    import ast

    return (
        isinstance(node, ast.Expr)
        and isinstance(getattr(node, "value", None), ast.Constant)
        and isinstance(node.value.value, str)
    )


def _leftover(rel: str, lines: list[str], occupied: set[int]) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[str] = []
    start: int | None = None

    def flush(end: int) -> None:
        nonlocal buf, start
        if buf and any(line.strip() for line in buf) and start is not None:
            chunks.append(Chunk(rel, start, end, "<module>", "\n".join(buf)))
        buf = []
        start = None

    for number, line in enumerate(lines, start=1):
        if number in occupied:
            flush(number - 1)
            continue
        if start is None:
            start = number
        buf.append(line)
    flush(len(lines))
    return chunks


def chunk_text(rel: str, text: str, max_lines: int = 80) -> list[Chunk]:
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        return []
    chunks: list[Chunk] = []
    buf: list[str] = []
    start = 1

    def flush(end: int) -> None:
        nonlocal buf, start
        if any(line.strip() for line in buf):
            chunks.append(Chunk(rel, start, end, "", "\n".join(buf)))
        buf = []

    for number, line in enumerate(lines, start=1):
        if HEADING_RE.match(line) and buf:
            flush(number - 1)
            start = number
        if not buf:
            start = number
        buf.append(line)
        if len(buf) >= max_lines:
            flush(number)
            start = number + 1
    if buf:
        flush(len(lines))
    return chunks


def _cap(chunks: list[Chunk]) -> tuple[list[Chunk], bool]:
    if len(chunks) <= MAX_CHUNKS:
        return chunks, False
    firsts: list[Chunk] = []
    rest: list[Chunk] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.path not in seen:
            firsts.append(chunk)
            seen.add(chunk.path)
        else:
            rest.append(chunk)
    return (firsts + rest)[:MAX_CHUNKS], True


def base_vector(token: str, dim: int) -> array.array:
    vec = array.array("f", [0.0] * dim)
    placed: set[int] = set()
    material = hashlib.sha256(token.encode("utf-8")).digest()
    round_index = 0
    while len(placed) < EMBED_NONZEROS:
        if round_index:
            material = hashlib.sha256(material + bytes([round_index])).digest()
        for offset in range(0, len(material) - 1, 2):
            pos = int.from_bytes(material[offset : offset + 2], "little") % dim
            if pos in placed:
                continue
            sign = 1.0 if material[offset] & 1 else -1.0
            vec[pos] = sign
            placed.add(pos)
            if len(placed) >= EMBED_NONZEROS:
                break
        round_index += 1
    return vec


def _l2(vec: array.array) -> array.array:
    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0.0:
        return vec
    return array.array("f", [value / norm for value in vec])


def _add(dest: array.array, src: array.array, scale: float = 1.0) -> None:
    for index, value in enumerate(src):
        dest[index] += value * scale


class BM25:
    def __init__(self, docs: list[list[str]]) -> None:
        self.n = len(docs)
        self.df: Counter[str] = Counter()
        self.tfs: list[Counter[str]] = []
        self.lengths: list[int] = []
        for doc in docs:
            counts = Counter(doc)
            self.tfs.append(counts)
            self.lengths.append(len(doc))
            self.df.update(counts.keys())
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0

    def score_all(self, query: list[str]) -> list[float]:
        scores = [0.0] * self.n
        if not self.n or self.avgdl == 0:
            return scores
        for term in set(query):
            df = self.df.get(term, 0)
            if not df:
                continue
            idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
            for index, counts in enumerate(self.tfs):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                denom = tf + K1 * (1.0 - B + B * self.lengths[index] / self.avgdl)
                scores[index] += idf * (tf * (K1 + 1.0)) / denom
        return scores


def _embed_keep(docs: list[list[str]]) -> set[str]:
    chunk_df: Counter[str] = Counter()
    for doc in docs:
        chunk_df.update(set(doc))
    n = len(docs)
    keep: set[str] = set()
    for token, count in chunk_df.items():
        if token in EMBED_STOP:
            continue
        if n >= 4 and count / n > 0.5:
            continue
        keep.add(token)
    return keep


def build_context(docs: list[list[str]], dim: int) -> dict[str, array.array] | None:
    vocab: set[str] = set()
    for doc in docs:
        vocab.update(doc)
    if len(vocab) > 30_000:
        return None
    keep = _embed_keep(docs)
    ctx = {token: array.array("f", [0.0] * dim) for token in vocab if token in keep}
    cache: dict[str, array.array] = {}

    def basis(token: str) -> array.array:
        cached = cache.get(token)
        if cached is None:
            cached = base_vector(token, dim)
            cache[token] = cached
        return cached

    for doc in docs:
        unique = [token for token in dict.fromkeys(doc) if token in keep]
        if len(unique) < 2:
            continue
        total = array.array("f", [0.0] * dim)
        bases = {token: basis(token) for token in unique}
        for token in unique:
            _add(total, bases[token])
        for token in unique:
            _add(ctx[token], total)
            _add(ctx[token], bases[token], -1.0)
    return {token: _l2(vec) for token, vec in ctx.items()}


def _occurrence(docs: list[list[str]]) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    for doc in docs:
        counts.update(doc)
    return counts, sum(counts.values())


def embed_doc(
    tokens: list[str],
    ctx: dict[str, array.array] | None,
    occurrence: Counter[str],
    total: int,
    dim: int,
) -> array.array:
    """SIF-weighted sum of context vectors.

    Tokens never seen in the repo use their base vector. Tokens that were
    dropped from the reflective vocabulary (structure words, very common
    words) contribute nothing. A missing context table means the hashing
    trick: every kept token uses its base vector.
    """
    vec = array.array("f", [0.0] * dim)
    counts = Counter(tokens)
    for token, tf in counts.items():
        if token in EMBED_STOP:
            continue
        if ctx is None:
            source = base_vector(token, dim)
        elif token in ctx:
            source = ctx[token]
        elif occurrence.get(token, 0) == 0:
            source = base_vector(token, dim)
        else:
            continue
        probability = (occurrence[token] / total) if total and token in occurrence else 0.0
        weight = (SIF_A / (SIF_A + probability)) * tf
        _add(vec, source, weight)
    return _l2(vec)


def _dot(left: array.array, right: array.array) -> float:
    return sum(a * b for a, b in zip(left, right))


def _ranks(scores: list[float]) -> dict[int, int]:
    order = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
    ranks: dict[int, int] = {}
    rank = 1
    for index in order:
        if scores[index] <= 0:
            continue
        ranks[index] = rank
        rank += 1
    return ranks


def fuse(
    bm25_scores: list[float],
    embed_scores: list[float],
    chunks: list[Chunk],
    limit: int = 8,
    per_file: int = 3,
    path_prefix: str | None = None,
) -> list[tuple[float, int]]:
    lexical = _ranks(bm25_scores)
    semantic = _ranks(embed_scores)
    fused: list[tuple[float, int]] = []
    for index in range(len(chunks)):
        score = 0.0
        if index in lexical:
            score += 1.0 / (RRF_K + lexical[index])
        if index in semantic:
            score += 1.0 / (RRF_K + semantic[index])
        if score > 0:
            fused.append((score, index))
    fused.sort(key=lambda item: item[0], reverse=True)
    picked: list[tuple[float, int]] = []
    counts: dict[str, int] = {}
    prefix = (path_prefix or "").replace("\\", "/").strip("/")
    for score, index in fused:
        path = chunks[index].path
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            continue
        if counts.get(path, 0) >= per_file:
            continue
        picked.append((score, index))
        counts[path] = counts.get(path, 0) + 1
        if len(picked) >= limit:
            break
    return picked


def confident(hits: list[Hit]) -> bool:
    if not hits:
        return False
    if len(hits) == 1:
        return hits[0].score > 0
    return hits[0].score >= hits[1].score * 1.15


def _dir_has_child(directory: Path) -> bool:
    try:
        return any(directory.iterdir())
    except OSError:
        return False


def repo_map(root: Path, max_depth: int = 3, max_entries: int = 200) -> str:
    root = root.resolve()
    patterns = load_patterns(root)
    lines: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if len(lines) >= max_entries:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        entries.sort(key=lambda item: (not item.is_dir(), item.name.lower()))
        for entry in entries:
            if len(lines) >= max_entries:
                return
            if entry.name in SKIP_DIRS or entry.is_symlink():
                continue
            try:
                rel = entry.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if ignored(rel, patterns):
                continue
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                if depth + 1 < max_depth:
                    walk(entry, depth + 1)
                elif _dir_has_child(entry):
                    lines.append(f"{indent}  ...")
            else:
                lines.append(f"{indent}{entry.name}")

    walk(root, 0)
    if len(lines) >= max_entries:
        lines.append("...")
    return "\n".join(lines)


class Index:
    def __init__(self, root: Path, dim: int = 128, index_dir: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.dim = dim
        self._index_dir_override = Path(index_dir).resolve() if index_dir is not None else None
        self.chunks: list[Chunk] = []
        self.capped = False
        self.file_count = 0
        self._docs: list[list[str]] = []
        self._bm25: BM25 | None = None
        self._ctx: dict[str, array.array] | None = None
        self._occurrence: Counter[str] = Counter()
        self._total = 0
        self._chunk_vecs: list[array.array] = []
        self._files: dict[str, int] = {}
        self._reflective = True
        self._ready = False

    @property
    def index_dir(self) -> Path:
        if self._index_dir_override is not None:
            return self._index_dir_override
        return self.root / ".bbagent" / "index"

    def refresh(self) -> None:
        if self._ready and not self.stale():
            return
        if self._load() and not self.stale():
            return
        self.build()
        self.save()

    def stale(self) -> bool:
        if not self._ready:
            return True
        return self._signature() != self._files

    def build(self) -> None:
        files = iter_source_files(self.root)
        chunks: list[Chunk] = []
        signature: dict[str, int] = {}
        used = 0
        for path in files:
            rel = path.resolve().relative_to(self.root).as_posix()
            try:
                signature[rel] = path.stat().st_mtime_ns
            except OSError:
                continue
            text = read_text(path)
            if not text:
                continue
            used += 1
            chunks.extend(chunk_file(rel, text))
        chunks, capped = _cap(chunks)
        self._install(chunks, signature, capped, files_seen=used)

    def _install(
        self,
        chunks: list[Chunk],
        signature: dict[str, int],
        capped: bool,
        files_seen: int,
        ctx: dict[str, array.array] | None | object = ...,
    ) -> None:
        self.chunks = chunks
        self.capped = capped
        self.file_count = files_seen
        self._files = signature
        self._docs = [tokenize(chunk.text) for chunk in chunks]
        self._bm25 = BM25(self._docs)
        self._occurrence, self._total = _occurrence(self._docs)
        if ctx is ...:
            self._ctx = build_context(self._docs, self.dim)
        else:
            self._ctx = ctx  # type: ignore[assignment]
        self._reflective = self._ctx is not None
        self._chunk_vecs = [
            embed_doc(doc, self._ctx, self._occurrence, self._total, self.dim) for doc in self._docs
        ]
        self._ready = True

    def save(self) -> None:
        if not self._ready:
            return
        directory = self.index_dir
        directory.mkdir(parents=True, exist_ok=True)
        vocab = sorted(self._ctx) if self._ctx is not None else []
        manifest = {
            "dim": self.dim,
            "capped": self.capped,
            "reflective": self._reflective,
            "file_count": self.file_count,
            "files": self._files,
            "vocab": vocab,
            "chunks": [
                {
                    "path": chunk.path,
                    "start": chunk.start,
                    "end": chunk.end,
                    "qualname": chunk.qualname,
                    "text": chunk.text,
                }
                for chunk in self.chunks
            ],
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8", newline="\n"
        )
        blob = array.array("f")
        if self._ctx is not None:
            for token in vocab:
                blob.extend(self._ctx[token])
        with (directory / "context.bin").open("wb") as handle:
            blob.tofile(handle)

    def _load(self) -> bool:
        manifest_path = self.index_dir / "manifest.json"
        blob_path = self.index_dir / "context.bin"
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if int(manifest.get("dim", 0)) != self.dim:
            return False
        chunks = [
            Chunk(
                path=item["path"],
                start=int(item["start"]),
                end=int(item["end"]),
                qualname=item.get("qualname", ""),
                text=item.get("text", ""),
            )
            for item in manifest.get("chunks", [])
        ]
        vocab = list(manifest.get("vocab", []))
        ctx: dict[str, array.array] | None = None
        if manifest.get("reflective", True):
            blob = array.array("f")
            try:
                with blob_path.open("rb") as handle:
                    blob.fromfile(handle, len(vocab) * self.dim)
            except (OSError, EOFError):
                return False
            ctx = {}
            for index, token in enumerate(vocab):
                start = index * self.dim
                ctx[token] = array.array("f", blob[start : start + self.dim])
        signature = {key: int(value) for key, value in manifest.get("files", {}).items()}
        self._install(
            chunks,
            signature,
            bool(manifest.get("capped", False)),
            int(manifest.get("file_count", 0)),
            ctx=ctx,
        )
        return True

    def _signature(self) -> dict[str, int]:
        signature: dict[str, int] = {}
        for path in iter_source_files(self.root):
            rel = path.resolve().relative_to(self.root).as_posix()
            try:
                signature[rel] = path.stat().st_mtime_ns
            except OSError:
                continue
        return signature

    def _prepare_query(self, query: str) -> tuple[list[str], array.array, list[float]] | None:
        if not self._ready or self._bm25 is None:
            return None
        tokens = tokenize(query)
        if not tokens:
            return None
        query_vec = embed_doc(tokens, self._ctx, self._occurrence, self._total, self.dim)
        lexical = self._bm25.score_all(tokens)
        return tokens, query_vec, lexical

    def _embed_scores(self, query_vec: array.array) -> list[float]:
        scores = []
        for vec in self._chunk_vecs:
            value = _dot(query_vec, vec)
            scores.append(value if value > 0 else 0.0)
        return scores

    def embed_hits(self, query: str, k: int = 8) -> list[Hit]:
        prepared = self._prepare_query(query)
        if prepared is None:
            return []
        _, query_vec, _ = prepared
        scores = self._embed_scores(query_vec)
        return self._top(scores, k)

    def search(self, query: str, k: int = 8, path_prefix: str | None = None) -> list[Hit]:
        prepared = self._prepare_query(query)
        if prepared is None:
            return []
        _, query_vec, lexical = prepared
        semantic = self._embed_scores(query_vec)
        picked = fuse(lexical, semantic, self.chunks, limit=k, path_prefix=path_prefix)
        return [self._hit(index, score) for score, index in picked]

    def _top(self, scores: list[float], k: int) -> list[Hit]:
        order = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
        hits = []
        for index in order:
            if scores[index] <= 0:
                break
            hits.append(self._hit(index, scores[index]))
            if len(hits) >= k:
                break
        return hits

    def _hit(self, index: int, score: float) -> Hit:
        chunk = self.chunks[index]
        return Hit(chunk.path, chunk.start, chunk.end, chunk.qualname, chunk.text, score)


def hit_dict(hit: Hit) -> dict:
    return {
        "path": hit.path,
        "start": hit.start,
        "end": hit.end,
        "qualname": hit.qualname,
        "text": hit.text,
        "score": hit.score,
    }


class ContextError(ValueError):
    pass


_ALIAS_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _clean_alias(name: str) -> str:
    cleaned = _ALIAS_UNSAFE.sub("-", name).strip(".-")
    return cleaned or "ctx"


def _make_alias(path: Path, used: set[str]) -> str:
    taken = {item.lower() for item in used}
    candidates = [_clean_alias(path.name), _clean_alias(f"{path.parent.name}-{path.name}")]
    for base in candidates:
        if base.lower() not in taken:
            return base
    base = candidates[0]
    index = 2
    while True:
        item = f"{base}-{index}"
        if item.lower() not in taken:
            return item
        index += 1


def build_sources(workspace: Path, context_dirs: list[str]) -> list[Source]:
    workspace = Path(workspace).resolve()
    sources = [
        Source(alias="", root=workspace, writable=True, prefix="", inside=True),
    ]
    used = {"workspace"}
    seen = {workspace}
    reserved = {(workspace / ".bbagent").resolve(), (workspace / ".git").resolve()}
    for raw in context_dirs:
        text = str(raw).strip()
        if not text:
            raise ContextError("context directory is empty")
        path = Path(text)
        if not path.is_absolute():
            path = workspace / path
        try:
            path = path.resolve()
        except OSError as exc:
            raise ContextError(f"cannot resolve context directory {text}: {exc}") from exc
        if not path.is_dir():
            raise ContextError(f"context directory does not exist: {text}")
        if path in seen:
            continue
        if path == workspace:
            continue
        if path in reserved:
            raise ContextError(f"context directory is reserved: {text}")
        if _is_under(workspace, path):
            raise ContextError(f"context directory contains the workspace: {text}")
        inside = _is_under(path, workspace)
        alias = _make_alias(path, used)
        used.add(alias)
        seen.add(path)
        if inside:
            prefix = path.relative_to(workspace).as_posix()
            sources.append(Source(alias=alias, root=path, writable=True, prefix=prefix, inside=True))
        else:
            sources.append(
                Source(alias=alias, root=path, writable=False, prefix=f"@{alias}", inside=False)
            )
    return sources


def split_alias(rel: str) -> tuple[str | None, str]:
    raw = (rel or "").strip().replace("\\", "/")
    if not raw.startswith("@"):
        return None, raw
    rest = raw[1:]
    if "/" in rest:
        alias, inner = rest.split("/", 1)
    else:
        alias, inner = rest, "."
    return alias.strip() or None, (inner.strip() or ".")


def find_source(sources: list[Source], alias: str) -> Source | None:
    for source in sources:
        if source.alias == alias:
            return source
    return None


def _rrf_merge(lists: list[list[Hit]], k: int, per_file: int) -> list[Hit]:
    scores: dict[tuple, float] = {}
    chosen: dict[tuple, Hit] = {}
    for ranked in lists:
        for rank, hit in enumerate(ranked, start=1):
            key = (hit.path, hit.start, hit.end)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            chosen[key] = hit
    ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
    picked: list[Hit] = []
    counts: dict[str, int] = {}
    for key in ordered:
        hit = chosen[key]
        if counts.get(hit.path, 0) >= per_file:
            continue
        picked.append(Hit(hit.path, hit.start, hit.end, hit.qualname, hit.text, scores[key]))
        counts[hit.path] = counts.get(hit.path, 0) + 1
        if len(picked) >= k:
            break
    return picked


class Corpus:
    def __init__(self, workspace: Path, sources: list[Source], dim: int) -> None:
        self.workspace = Path(workspace).resolve()
        self.sources = sources
        self.indexes: dict[str, Index] = {}
        workspace_index = Index(self.workspace, dim=dim)
        workspace_index.refresh()
        self.indexes[""] = workspace_index
        for source in sources:
            if not source.alias or source.inside:
                continue
            extra = Index(
                source.root,
                dim=dim,
                index_dir=self.workspace / ".bbagent" / "index" / "context" / source.alias,
            )
            extra.refresh()
            self.indexes[source.alias] = extra

    @property
    def workspace_index(self) -> Index:
        return self.indexes[""]

    def refresh_stale(self) -> None:
        for index in self.indexes.values():
            if index.stale():
                index.refresh()

    def search(self, query: str, k: int = 8) -> list[Hit]:
        workspace_index = self.workspace_index
        lists: list[list[Hit]] = [workspace_index.search(query, k=k)]
        for source in self.sources:
            if not source.alias:
                continue
            if source.inside:
                lists.append(workspace_index.search(query, k=k, path_prefix=source.prefix))
                continue
            extra = self.indexes[source.alias]
            lists.append(
                [
                    Hit(
                        source.prefixed(hit.path),
                        hit.start,
                        hit.end,
                        hit.qualname,
                        hit.text,
                        hit.score,
                    )
                    for hit in extra.search(query, k=k)
                ]
            )
        return _rrf_merge(lists, k=k, per_file=3)

    def describe(self) -> list[dict]:
        return [
            {
                "alias": source.alias or "workspace",
                "path": str(source.root),
                "writable": source.writable,
            }
            for source in self.sources
        ]

    def maps(self) -> dict[str, str]:
        return {
            source.alias: repo_map(source.root, max_depth=2, max_entries=40)
            for source in self.sources
            if source.alias
        }

    def capped(self) -> bool:
        return any(index.capped for index in self.indexes.values())

    def empty(self) -> bool:
        return all(not index.chunks for index in self.indexes.values())

