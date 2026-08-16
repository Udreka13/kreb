"""The whole-repository symbol index.

This is the ground truth every other guarantee rests on: anchor validation,
staleness, and the repo map all read from here, and none of them needs a model
call. It is also the *only* thing that can catch a fabricated anchor, so its
completeness matters more than its cleverness.

Deliberately a definition index, not a resolver. Cross-file name resolution is
undecidable in general (`__getattr__` module hooks, `importlib`, TS `export *`
chains), and a resolver that is wrong in a long tail is worse than none: it
emits a confident anchor pointing at the wrong definition, which validation
cannot catch because the symbol resolves. Ambiguity is reported instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from kreb.index.hashes import shape_hash, signature_hash, text_hash
from kreb.index.symbols import Symbol, extract_symbols
from kreb.repo.access import Repository

# Extension -> tree-sitter language. Only these get symbol-level anchoring;
# everything else is indexed at file granularity and reported as degraded.
LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
}

_IMPORT_NODES = {
    "python": ("import_statement", "import_from_statement"),
    "typescript": ("import_statement", "export_statement"),
    "tsx": ("import_statement", "export_statement"),
    "javascript": ("import_statement", "export_statement"),
}


def language_for(path: str) -> str | None:
    for suffix, lang in LANGUAGES.items():
        if path.endswith(suffix):
            return lang
    return None


def symbol_ref(path: str, qualname: str) -> str:
    """The canonical anchor format.

    `path#Class.method`, not `path#method` — a bare name is ambiguous for
    methods, overloads and every `__init__` in the repository. This appears in
    every artifact kreb produces and is painful to migrate, so it is defined
    once, here.
    """
    return f"{path}#{qualname}"


@dataclass
class IndexedSymbol:
    """A definition plus the three hashes, detached from the parse tree.

    Nodes are not retained: they hold a reference to the whole tree, and keeping
    thousands alive across a run is a memory leak with no upside once the hashes
    are computed.
    """

    ref: str
    path: str
    name: str
    qualname: str
    kind: str
    language: str
    start_line: int
    end_line: int
    text_hash: str
    shape_hash: str
    signature_hash: str

    @classmethod
    def from_symbol(cls, path: str, sym: Symbol) -> IndexedSymbol:
        return cls(
            ref=symbol_ref(path, sym.qualname),
            path=path,
            name=sym.name,
            qualname=sym.qualname,
            kind=sym.kind,
            language=sym.language,
            start_line=sym.start_line,
            end_line=sym.end_line,
            text_hash=text_hash(sym),
            shape_hash=shape_hash(sym),
            signature_hash=signature_hash(sym),
        )


@dataclass
class FileEntry:
    path: str
    language: str | None
    loc: int
    degraded: bool
    imports: tuple[str, ...] = ()


@dataclass
class RepoIndex:
    """Everything derivable from the repository without a model call."""

    sha: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    symbols: dict[str, IndexedSymbol] = field(default_factory=dict)
    # name -> refs, for ambiguity-aware lookup by bare name.
    by_name: dict[str, list[str]] = field(default_factory=dict)

    # -- the interface the store's trace validation consumes ---------------

    def current_hashes(self) -> dict[str, str]:
        """`ref -> text_hash` for every known symbol."""
        return {ref: sym.text_hash for ref, sym in self.symbols.items()}

    # -- anchor validation -------------------------------------------------

    def resolve(self, ref: str) -> IndexedSymbol | None:
        return self.symbols.get(ref)

    def find_by_name(self, name: str) -> list[IndexedSymbol]:
        """Every symbol whose bare or qualified name matches.

        Returning a list rather than a best guess is the whole point: an
        ambiguous anchor is reported as ambiguous, never silently resolved to
        whichever definition happened to be indexed first.
        """
        return [self.symbols[r] for r in self.by_name.get(name, [])]

    def anchor_status(self, ref: str) -> str:
        """Classify an anchor: `resolved`, `misplaced`, `ambiguous` or `fabricated`.

        `misplaced` and `fabricated` are different failures with different
        causes — a real symbol cited at the wrong path versus a symbol that
        exists nowhere — and are reported separately.
        """
        if ref in self.symbols:
            return "resolved"
        _, _, qualname = ref.partition("#")
        if not qualname:
            return "fabricated"
        matches = self.by_name.get(qualname, []) or self.by_name.get(
            qualname.rpartition(".")[2], []
        )
        if not matches:
            return "fabricated"
        return "misplaced" if len(matches) == 1 else "ambiguous"

    def moved_to(self, ref: str, known_hash: str) -> str | None:
        """Find where a symbol went, by content.

        A dangling anchor whose text hash still exists elsewhere means the
        function moved, not that the claim was wrong. Treating that as a
        fabricated anchor would be both incorrect and user-hostile.
        """
        for candidate, sym in self.symbols.items():
            if candidate != ref and sym.text_hash == known_hash:
                return candidate
        return None


def _import_targets(root: Node, source: bytes, language: str) -> list[str]:
    """Module-granularity import edges.

    Module granularity is deliberate: it is what the map needs, it is what
    tree-sitter can give exactly, and going finer would require the resolver
    this design specifically avoids.
    """
    wanted = _IMPORT_NODES.get(language, ())
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in wanted:
            for child in node.children:
                if child.type in ("dotted_name", "string", "relative_import"):
                    raw = source[child.start_byte : child.end_byte].decode("utf-8", "replace")
                    cleaned = raw.strip("\"'")
                    if cleaned:
                        targets.append(cleaned)
        for child in node.children:
            walk(child)

    walk(root)
    return targets


def build_index(repo: Repository, *, include_vendored: bool = False) -> RepoIndex:
    """Walk the repository at its pinned SHA and index every definition.

    No model calls. This function is on the hot path of every run and must stay
    that way — see `tests/test_map_is_free.py`.
    """
    index = RepoIndex(sha=repo.sha)

    for path in repo.files(include_vendored=include_vendored):
        language = language_for(path)
        try:
            source = repo.read(path)
        except Exception:
            # An unreadable blob (submodule pointer, symlink) is not fatal.
            continue

        loc = source.count(b"\n") + 1 if source else 0

        if language is None:
            index.files[path] = FileEntry(path=path, language=None, loc=loc, degraded=True)
            repo.caps.degraded_files += 1
            continue

        parser = get_parser(language)
        tree = parser.parse(source)

        index.files[path] = FileEntry(
            path=path,
            language=language,
            loc=loc,
            degraded=False,
            imports=tuple(_import_targets(tree.root_node, source, language)),
        )

        for sym in extract_symbols(source, language):
            indexed = IndexedSymbol.from_symbol(path, sym)
            index.symbols[indexed.ref] = indexed
            index.by_name.setdefault(indexed.name, []).append(indexed.ref)
            if indexed.qualname != indexed.name:
                index.by_name.setdefault(indexed.qualname, []).append(indexed.ref)

    repo.caps.languages = sorted(
        {f.language for f in index.files.values() if f.language is not None}
    )
    return index
