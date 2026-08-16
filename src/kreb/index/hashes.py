"""The three symbol hashes that drive staleness.

Why three, and why not the obvious one: the S-expression of a tree-sitter node
carries node *types and field names only* — never token text. `(integer)` is
identical whether the literal is 3 or 5; `(comparison_operator ...)` is identical
for `<` and `<=`. Hashing it therefore misses almost every semantic edit while
firing on an added comment. See architecture.md §2 for the measured table.

    text_hash       drives staleness           — every literal, operator, name
    shape_hash      classifies a fired change  — cosmetic-or-constant vs structural
    signature_hash  drives caller invalidation — interface only, body excluded

`tests/test_semantic_change.py` is the executable specification for all three.
"""

from __future__ import annotations

import hashlib

from tree_sitter import Node

# Leaf node types whose text never affects meaning.
_IGNORED_LEAVES = frozenset({"comment"})

# Separator between tokens, so that `ab` `c` and `a` `bc` cannot collide.
_SEP = b"\x00"


def _leaf_tokens(
    node: Node,
    source: bytes,
    *,
    skip_ids: frozenset[int] = frozenset(),
) -> list[bytes]:
    """Collect the source text of every leaf in `node`, in order.

    Ignores whitespace and formatting (they are not leaves), skips comments, and
    skips any subtree whose root id is in `skip_ids` — that is how the body is
    excluded from a signature.
    """
    tokens: list[bytes] = []

    def walk(n: Node) -> None:
        if n.id in skip_ids:
            return
        if n.child_count == 0:
            if n.type not in _IGNORED_LEAVES:
                tokens.append(source[n.start_byte : n.end_byte])
            return
        for child in n.children:
            walk(child)

    walk(node)
    return tokens


def _digest(parts: list[bytes]) -> str:
    return hashlib.sha256(_SEP.join(parts)).hexdigest()


def text_hash(symbol) -> str:
    """Hash of the token stream. **This is the staleness signal.**

    Insensitive to reformatting and comments; sensitive to every literal,
    operator, identifier, annotation and decorator.
    """
    return _digest(_leaf_tokens(symbol.hash_node, symbol.source))


def shape_hash(symbol) -> str:
    """Hash of the S-expression — structure only, no token text.

    Never use this for staleness. Its job is to classify a change that
    `text_hash` already detected: shape unchanged means the edit was cosmetic or
    a constant, which renders as "changed"; shape changed means structural,
    which renders as "may be wrong".
    """
    return hashlib.sha256(str(symbol.hash_node).encode("utf-8")).hexdigest()


def signature_hash(symbol) -> str:
    """Hash of the public interface: decorators, name, parameters, return type.

    Excludes the body, so that a body edit invalidates sections citing this
    symbol while leaving sections citing its *callers* alone. A signature change
    invalidates both.
    """
    body = symbol.body_node
    skip = frozenset({body.id}) if body is not None else frozenset()
    return _digest(_leaf_tokens(symbol.hash_node, symbol.source, skip_ids=skip))
