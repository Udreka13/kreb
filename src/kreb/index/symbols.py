"""Symbol extraction — the definition index.

Deliberately a *definition* index, not a resolver. Anchor validation, staleness,
the repo map and caller-finding are all served by definitions plus honest
ambiguity reporting; only extracted call-path diagrams need true cross-file
resolution, and those come late. A hand-rolled resolver's failure mode is worse
than not having one: it emits a confident anchor at the wrong definition, which
Gate A cannot catch because the symbol resolves. See architecture.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

# Node types that introduce a named symbol, per language.
_DEFINITIONS: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "typescript": {
        "function_declaration": "function",
        "method_definition": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
}
_DEFINITIONS["tsx"] = _DEFINITIONS["typescript"]
_DEFINITIONS["javascript"] = {
    k: v for k, v in _DEFINITIONS["typescript"].items() if k != "interface_declaration"
}

# Wrappers that must be folded into the symbol so their content is hashed.
# A Python decorator lives on `decorated_definition`, *outside* the
# `function_definition` — hashing only the inner node makes `@cached` -> `@retry`
# invisible, which is one of the misses this whole design exists to prevent.
_WRAPPERS = frozenset({"decorated_definition", "export_statement"})

# Node types that create a naming scope, for qualified names.
_SCOPES = frozenset(
    {"class_definition", "class_declaration", "interface_declaration", "function_definition"}
)


@dataclass(frozen=True)
class Symbol:
    """One definition, with everything the hashes need.

    `node` is the definition itself; `hash_node` may be an enclosing wrapper so
    that decorators and `export` participate in the hash.
    """

    name: str
    qualname: str
    kind: str
    language: str
    node: Node = field(repr=False)
    hash_node: Node = field(repr=False)
    body_node: Node | None = field(repr=False)
    source: bytes = field(repr=False)

    @property
    def start_line(self) -> int:
        return self.hash_node.start_point[0] + 1

    @property
    def end_line(self) -> int:
        return self.hash_node.end_point[0] + 1


def _name_of(node: Node, source: bytes) -> str | None:
    ident = node.child_by_field_name("name")
    if ident is None:
        return None
    return source[ident.start_byte : ident.end_byte].decode("utf-8", "replace")


def _hash_node_for(node: Node) -> Node:
    """Walk outward through wrappers so decorators/export are inside the hash."""
    current = node
    while (
        current.parent is not None
        and current.parent.type in _WRAPPERS
        # Only fold in a wrapper that exists *for* this node, not a shared one.
        and current.parent.start_byte <= current.start_byte
    ):
        current = current.parent
    return current


def extract_symbols(source: bytes, language: str) -> list[Symbol]:
    """Extract every named definition from `source`.

    Names are both simple (`should_retry`) and qualified
    (`RetryPolicy.should_retry`); the qualified form is what a `SymbolRef`
    anchors to, since `path#name` alone is ambiguous for methods and overloads.
    """
    try:
        definitions = _DEFINITIONS[language]
    except KeyError as exc:  # pragma: no cover - guarded by callers
        raise ValueError(f"unsupported language: {language!r}") from exc

    parser = get_parser(language)
    tree = parser.parse(source)
    found: list[Symbol] = []

    def walk(node: Node, scope: tuple[str, ...]) -> None:
        inner_scope = scope
        if node.type in definitions:
            name = _name_of(node, source)
            if name is not None:
                qualname = ".".join((*scope, name))
                found.append(
                    Symbol(
                        name=name,
                        qualname=qualname,
                        kind=definitions[node.type],
                        language=language,
                        node=node,
                        hash_node=_hash_node_for(node),
                        body_node=node.child_by_field_name("body"),
                        source=source,
                    )
                )
                if node.type in _SCOPES:
                    inner_scope = (*scope, name)

        for child in node.children:
            walk(child, inner_scope)

    walk(tree.root_node, ())
    return found
