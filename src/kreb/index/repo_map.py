"""The repository map — deterministic, and that is the whole point.

PRD §6.1 specifies progressive leaf-to-root summarization: summarize each module
from its symbols and its children's summaries. Costed against a 50,000-file
repository that is 50,000+ model calls *before a single research question is
asked* — hours of wall clock and several times the entire per-document budget,
spent on directories the agent will never visit. It is the thing that dies first
at scale, and the PRD does not cost it.

So the map is model-free: directory tree, symbol counts, an import-graph
centrality ranking and LOC — all already available from the index, at zero cost,
and incrementally updatable.

Module *summaries* remain valuable but become a lazily-populated cache: the
agent asks for a summary of a directory it is actually drilling into, and that
call is memoized as its own node keyed on the child symbols' hashes. You pay for
the twenty directories visited, not the five thousand that exist.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from kreb.index.repo_index import RepoIndex


@dataclass
class DirectoryNode:
    path: str
    files: int = 0
    symbols: int = 0
    loc: int = 0
    languages: set[str] = field(default_factory=set)
    children: list[str] = field(default_factory=list)


@dataclass
class RepoMap:
    """A navigable, model-free summary of repository shape."""

    sha: str
    directories: dict[str, DirectoryNode]
    central_symbols: list[tuple[str, float]]
    entry_points: list[str]
    total_files: int
    total_symbols: int
    total_loc: int
    degraded_files: int

    def render(self, *, max_dirs: int = 60, max_symbols: int = 40) -> str:
        """A compact text rendering — the stable prefix of every research call.

        Stability matters more than density here: this text is the cached prompt
        prefix, so it must not reorder between calls or the cache affinity that
        makes the cost model work is lost.
        """
        lines = [f"# Repository map @ {self.sha[:12]}", ""]
        lines.append(
            f"{self.total_files} files, {self.total_symbols} symbols, "
            f"{self.total_loc} lines"
            + (f", {self.degraded_files} files without symbol support" if self.degraded_files else "")
        )
        lines.append("")

        lines.append("## Structure")
        ranked = sorted(
            self.directories.values(), key=lambda d: (-d.symbols, d.path)
        )[:max_dirs]
        for node in sorted(ranked, key=lambda d: d.path):
            langs = "/".join(sorted(node.languages)) if node.languages else "-"
            lines.append(
                f"  {node.path or '.'}  ({node.files} files, {node.symbols} symbols, {langs})"
            )

        if self.entry_points:
            lines.append("")
            lines.append("## Entry points")
            for path in self.entry_points[:15]:
                lines.append(f"  {path}")

        lines.append("")
        lines.append("## Most depended-upon symbols")
        for ref, score in self.central_symbols[:max_symbols]:
            lines.append(f"  {ref}  ({score:.3f})")

        return "\n".join(lines)


_ENTRY_HINTS = (
    "main.py",
    "__main__.py",
    "app.py",
    "cli.py",
    "server.py",
    "index.ts",
    "index.js",
    "main.ts",
    "app.ts",
    "setup.py",
    "conftest.py",
)


def _directory_of(path: str) -> str:
    return path.rpartition("/")[0]


def _centrality(index: RepoIndex) -> list[tuple[str, float]]:
    """Rank symbols by how much of the repository points at them.

    A deliberately simple, deterministic proxy rather than true pagerank over a
    call graph: a symbol's score is how many *other modules* import its module,
    scaled by whether the symbol is exported-looking. Cross-file call resolution
    is exactly the undecidable problem this design avoids, so the map does not
    pretend to it — this ranking exists to help an agent choose where to look,
    not to make claims.
    """
    module_inbound: dict[str, int] = defaultdict(int)
    # Map plausible module names to the files that could define them.
    stem_to_paths: dict[str, list[str]] = defaultdict(list)
    for path in index.files:
        stem = path.rpartition("/")[2]
        for suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if stem.endswith(suffix):
                stem_to_paths[stem[: -len(suffix)]].append(path)
                break

    for entry in index.files.values():
        for target in entry.imports:
            leaf = target.replace("\\", "/").rpartition("/")[2].rpartition(".")[2] or target
            for candidate in stem_to_paths.get(leaf, []):
                if candidate != entry.path:
                    module_inbound[candidate] += 1

    scored: list[tuple[str, float]] = []
    for ref, sym in index.symbols.items():
        inbound = module_inbound.get(sym.path, 0)
        # Prefer top-level definitions; a nested helper is rarely the thing to
        # explain first.
        depth_penalty = 1.0 / (1 + sym.qualname.count("."))
        size = max(1, sym.end_line - sym.start_line)
        score = (inbound + 1) * depth_penalty * (1.0 + min(size, 200) / 200.0)
        if not sym.name.startswith("_"):
            score *= 1.5
        scored.append((ref, score))

    scored.sort(key=lambda item: (-item[1], item[0]))
    if scored:
        top = scored[0][1] or 1.0
        scored = [(ref, score / top) for ref, score in scored]
    return scored


def build_map(index: RepoIndex) -> RepoMap:
    """Build the repository map. Makes no model calls, by construction."""
    directories: dict[str, DirectoryNode] = {}
    symbols_per_path: dict[str, int] = defaultdict(int)

    for sym in index.symbols.values():
        symbols_per_path[sym.path] += 1

    for path, entry in index.files.items():
        directory = _directory_of(path)
        node = directories.get(directory)
        if node is None:
            node = DirectoryNode(path=directory)
            directories[directory] = node
        node.files += 1
        node.loc += entry.loc
        node.symbols += symbols_per_path.get(path, 0)
        if entry.language:
            node.languages.add(entry.language)

    # Link parents so the tree can be walked, creating implicit ancestors.
    for directory in list(directories):
        parent = _directory_of(directory)
        if directory and parent != directory:
            if parent not in directories:
                directories[parent] = DirectoryNode(path=parent)
            if directory not in directories[parent].children:
                directories[parent].children.append(directory)

    entry_points = sorted(
        path for path in index.files if path.rpartition("/")[2] in _ENTRY_HINTS
    )

    return RepoMap(
        sha=index.sha,
        directories=directories,
        central_symbols=_centrality(index),
        entry_points=entry_points,
        total_files=len(index.files),
        total_symbols=len(index.symbols),
        total_loc=sum(f.loc for f in index.files.values()),
        degraded_files=sum(1 for f in index.files.values() if f.degraded),
    )
