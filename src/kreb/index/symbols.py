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

# Function bodies, inside which a binding is a local variable rather than a
# citable definition. Constants are indexed at module and class scope only.
_FUNCTION_BODIES = frozenset(
    {"function_definition", "function_declaration", "method_definition", "arrow_function"}
)

# Named bindings that are not functions or classes but are perfectly citable:
# `MAX_RETRIES = 3`, `export const CONFIG = {...}`, a TS class field. Omitting
# these makes a section that cites one fail validation as a *fabricated anchor*
# — a hard Gate A failure — purely because the index was narrower than the
# repository's set of named things.
_BINDINGS: dict[str, tuple[str, ...]] = {
    # tree-sitter-python 0.25 emits a bare `assignment` at module and class
    # scope; older grammars wrapped it in `expression_statement`. Accept both so
    # a grammar bump cannot silently empty the constant index.
    "python": ("assignment", "expression_statement"),
    "typescript": ("lexical_declaration", "public_field_definition", "variable_declaration"),
    "tsx": ("lexical_declaration", "public_field_definition", "variable_declaration"),
    "javascript": ("lexical_declaration", "public_field_definition", "variable_declaration"),
}


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
    bindings = _BINDINGS.get(language, ())
    found: list[Symbol] = []

    def add(node: Node, name: str, kind: str, scope: tuple[str, ...]) -> None:
        found.append(
            Symbol(
                name=name,
                qualname=".".join((*scope, name)),
                kind=kind,
                language=language,
                node=node,
                hash_node=_hash_node_for(node),
                body_node=node.child_by_field_name("body"),
                source=source,
            )
        )

    def walk(node: Node, scope: tuple[str, ...], in_function: bool) -> None:
        inner_scope = scope
        inner_in_function = in_function

        if node.type in definitions:
            name = _name_of(node, source)
            if name is not None:
                add(node, name, definitions[node.type], scope)
                if node.type in _SCOPES:
                    inner_scope = (*scope, name)
        elif node.type in bindings and not in_function:
            for name, target in _binding_names(node, source, language):
                add(target, name, "constant", scope)

        if node.type in _FUNCTION_BODIES:
            inner_in_function = True

        for child in node.children:
            walk(child, inner_scope, inner_in_function)

    walk(tree.root_node, (), False)
    return found


def _binding_names(node: Node, source: bytes, language: str) -> list[tuple[str, Node]]:
    """Names bound by an assignment or declaration, with the node to hash.

    Only simple identifier targets are indexed. Destructuring, subscripts and
    attribute targets (`obj.attr = ...`) are deliberately skipped: they are not
    definitions, and inventing anchors for them would create the very fabricated
    references validation exists to catch.
    """
    results: list[tuple[str, Node]] = []

    def text(n: Node) -> str:
        return source[n.start_byte : n.end_byte].decode("utf-8", "replace")

    if language == "python":
        candidates = [node] if node.type == "assignment" else list(node.children)
        for child in candidates:
            if child.type != "assignment":
                continue
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                results.append((text(left), child))
        return results

    # TypeScript / JavaScript
    if node.type == "public_field_definition":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.type in ("property_identifier", "identifier"):
            results.append((text(name_node), node))
        return results

    for child in node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None and name_node.type == "identifier":
            results.append((text(name_node), child))
    return results
