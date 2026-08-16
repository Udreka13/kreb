"""Detector for FATAL bug #1 — staleness hashing blind to semantic change.

The originally-recommended staleness primitive, sha256 of the tree-sitter
S-expression, carries node *types and field names only* — no token text. It is
therefore blind to literal changes, operator swaps, renamed callees, decorator
swaps and type-annotation changes, while firing spuriously on added comments.

The trap this corpus exists to prevent: Gate A's synthetic-refactor test uses a
symbol rename, which perturbs enough structure that the broken primitive passes
it. A single rename case is NOT a test of this mechanism. Every row below is.

See architecture.md §2 and §9.
"""

from __future__ import annotations

import pytest

from kreb.index.hashes import shape_hash, signature_hash, text_hash
from kreb.index.symbols import extract_symbols

# --------------------------------------------------------------------------
# Python corpus
# --------------------------------------------------------------------------

PY_BASE = b'''
class RetryPolicy:
    @cached
    def should_retry(self, attempt: int, limit: int = 3) -> bool:
        """Retry up to three times."""
        delay = 3
        if attempt == limit:
            return backoff_linear(delay) and True
        return attempt < limit
'''

# (label, mutated source, must_fire)
PY_CASES: list[tuple[str, bytes, bool]] = [
    # --- must NOT fire: cosmetic only -------------------------------------
    ("identical", PY_BASE, False),
    ("reindented", PY_BASE.replace(b"        delay = 3", b"        delay =  3"), False),
    ("blank line added", PY_BASE.replace(b"        delay = 3", b"\n        delay = 3"), False),
    ("comment added", PY_BASE.replace(b"        delay = 3", b"        # tuned in PR#412\n        delay = 3"), False),
    ("comment edited", PY_BASE.replace(b"    @cached", b"    # see ADR-7\n    @cached"), False),
    # --- must fire: semantic ----------------------------------------------
    ("int literal 3 -> 5", PY_BASE.replace(b"delay = 3", b"delay = 5"), True),
    ("default arg 3 -> 5", PY_BASE.replace(b"limit: int = 3", b"limit: int = 5"), True),
    ("comparison < -> <=", PY_BASE.replace(b"attempt < limit", b"attempt <= limit"), True),
    ("equality == -> !=", PY_BASE.replace(b"attempt == limit", b"attempt != limit"), True),
    ("boolean and -> or", PY_BASE.replace(b") and True", b") or True"), True),
    ("callee renamed", PY_BASE.replace(b"backoff_linear", b"backoff_exponential"), True),
    ("decorator swapped", PY_BASE.replace(b"@cached", b"@retry"), True),
    ("type annotation changed", PY_BASE.replace(b"attempt: int", b"attempt: float"), True),
    ("return annotation changed", PY_BASE.replace(b"-> bool:", b"-> int:"), True),
    ("param renamed", PY_BASE.replace(b"attempt", b"tries"), True),
    ("param added", PY_BASE.replace(b"limit: int = 3)", b"limit: int = 3, jitter: bool = False)"), True),
    ("statement added", PY_BASE.replace(b"        return attempt", b"        log(attempt)\n        return attempt"), True),
    ("statement removed", PY_BASE.replace(b"        delay = 3\n", b""), True),
    ("docstring text changed", PY_BASE.replace(b"up to three times", b"up to five times"), True),
]

# --------------------------------------------------------------------------
# TypeScript corpus — the reviewer found TS-specific misses the Python
# corpus cannot surface (type annotations, && / ||).
# --------------------------------------------------------------------------

TS_BASE = b'''
export class RetryPolicy {
  shouldRetry(attempt: number, limit: number = 3): boolean {
    const delay: number = 3;
    if (attempt === limit) {
      return backoffLinear(delay) && true;
    }
    return attempt < limit;
  }
}
'''

TS_CASES: list[tuple[str, bytes, bool]] = [
    ("identical", TS_BASE, False),
    ("reformatted", TS_BASE.replace(b"const delay: number = 3;", b"const delay: number   =   3;"), False),
    ("comment added", TS_BASE.replace(b"    const delay", b"    // tuned in PR#412\n    const delay"), False),
    ("literal 3 -> 5", TS_BASE.replace(b"= 3;", b"= 5;"), True),
    ("strict eq === -> !==", TS_BASE.replace(b"attempt === limit", b"attempt !== limit"), True),
    ("logical && -> ||", TS_BASE.replace(b") && true", b") || true"), True),
    ("comparison < -> <=", TS_BASE.replace(b"attempt < limit", b"attempt <= limit"), True),
    ("param type number -> string", TS_BASE.replace(b"attempt: number", b"attempt: string"), True),
    ("return type changed", TS_BASE.replace(b"): boolean {", b"): number {"), True),
    ("callee renamed", TS_BASE.replace(b"backoffLinear", b"backoffExponential"), True),
    ("statement added", TS_BASE.replace(b"    return attempt", b"    log(attempt);\n    return attempt"), True),
]


def _sym(src: bytes, lang: str, name: str):
    """Extract one named symbol, failing loudly if the corpus drifts."""
    syms = {s.name: s for s in extract_symbols(src, lang)}
    assert name in syms, f"{name!r} not extracted from {lang}; got {sorted(syms)}"
    return syms[name]


# --------------------------------------------------------------------------
# The primary contract: text_hash drives staleness.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,mutated,must_fire", PY_CASES, ids=[c[0] for c in PY_CASES])
def test_python_text_hash(label: str, mutated: bytes, must_fire: bool) -> None:
    base = _sym(PY_BASE, "python", "should_retry")
    other = _sym(mutated, "python", "should_retry" if label != "param renamed" else "should_retry")
    fired = text_hash(base) != text_hash(other)
    assert fired is must_fire, (
        f"{label}: text_hash {'fired' if fired else 'did not fire'}, expected "
        f"{'fire' if must_fire else 'no fire'}"
    )


@pytest.mark.parametrize("label,mutated,must_fire", TS_CASES, ids=[c[0] for c in TS_CASES])
def test_typescript_text_hash(label: str, mutated: bytes, must_fire: bool) -> None:
    base = _sym(TS_BASE, "typescript", "shouldRetry")
    other = _sym(mutated, "typescript", "shouldRetry")
    fired = text_hash(base) != text_hash(other)
    assert fired is must_fire, (
        f"{label}: text_hash {'fired' if fired else 'did not fire'}, expected "
        f"{'fire' if must_fire else 'no fire'}"
    )


# --------------------------------------------------------------------------
# The regression guard: prove the OLD primitive is broken, so that if anyone
# ever reintroduces shape-only hashing for staleness, this fails loudly.
# --------------------------------------------------------------------------

SHAPE_BLIND = [
    ("int literal 3 -> 5", PY_BASE.replace(b"delay = 3", b"delay = 5")),
    ("comparison < -> <=", PY_BASE.replace(b"attempt < limit", b"attempt <= limit")),
    ("equality == -> !=", PY_BASE.replace(b"attempt == limit", b"attempt != limit")),
    ("callee renamed", PY_BASE.replace(b"backoff_linear", b"backoff_exponential")),
    ("decorator swapped", PY_BASE.replace(b"@cached", b"@retry")),
    ("type annotation changed", PY_BASE.replace(b"attempt: int", b"attempt: float")),
]


@pytest.mark.parametrize("label,mutated", SHAPE_BLIND, ids=[c[0] for c in SHAPE_BLIND])
def test_shape_hash_is_blind_to_these(label: str, mutated: bytes) -> None:
    """Documents WHY shape_hash must never drive staleness.

    If one of these starts failing, tree-sitter changed its S-expression to
    include token text — revisit architecture.md §2 before celebrating.
    """
    base = _sym(PY_BASE, "python", "should_retry")
    other = _sym(mutated, "python", "should_retry")
    assert shape_hash(base) == shape_hash(other), (
        f"{label}: shape_hash unexpectedly distinguishes these. "
        "The premise of the token-hash fix may have changed."
    )


def test_shape_hash_false_positive_on_comment() -> None:
    """The other half of shape_hash's unfitness: it fires on a pure comment."""
    base = _sym(PY_BASE, "python", "should_retry")
    commented = _sym(
        PY_BASE.replace(b"        delay = 3", b"        # tuned in PR#412\n        delay = 3"),
        "python",
        "should_retry",
    )
    assert shape_hash(base) != shape_hash(commented)
    assert text_hash(base) == text_hash(commented)


# --------------------------------------------------------------------------
# shape_hash's legitimate job: classifying a fired change as cosmetic-or-
# constant vs structural, which licenses "changed" instead of "may be wrong".
# --------------------------------------------------------------------------

def test_shape_hash_classifies_constant_vs_structural() -> None:
    base = _sym(PY_BASE, "python", "should_retry")

    const_only = _sym(PY_BASE.replace(b"delay = 3", b"delay = 5"), "python", "should_retry")
    assert text_hash(base) != text_hash(const_only)
    assert shape_hash(base) == shape_hash(const_only), "constant change should not alter shape"

    structural = _sym(
        PY_BASE.replace(b"        return attempt", b"        log(attempt)\n        return attempt"),
        "python",
        "should_retry",
    )
    assert text_hash(base) != text_hash(structural)
    assert shape_hash(base) != shape_hash(structural), "added statement should alter shape"


# --------------------------------------------------------------------------
# signature_hash drives CALLER invalidation: body edits must not disturb it,
# interface edits must.
# --------------------------------------------------------------------------

SIGNATURE_STABLE = [
    ("body literal changed", PY_BASE.replace(b"delay = 3", b"delay = 5")),
    ("body statement added", PY_BASE.replace(b"        return attempt", b"        log(attempt)\n        return attempt")),
    ("docstring changed", PY_BASE.replace(b"up to three times", b"up to five times")),
]

SIGNATURE_CHANGED = [
    ("param added", PY_BASE.replace(b"limit: int = 3)", b"limit: int = 3, jitter: bool = False)")),
    ("param type changed", PY_BASE.replace(b"attempt: int", b"attempt: float")),
    ("return type changed", PY_BASE.replace(b"-> bool:", b"-> int:")),
    ("default value changed", PY_BASE.replace(b"limit: int = 3", b"limit: int = 5")),
    ("decorator swapped", PY_BASE.replace(b"@cached", b"@retry")),
]


@pytest.mark.parametrize("label,mutated", SIGNATURE_STABLE, ids=[c[0] for c in SIGNATURE_STABLE])
def test_signature_stable_under_body_edits(label: str, mutated: bytes) -> None:
    base = _sym(PY_BASE, "python", "should_retry")
    other = _sym(mutated, "python", "should_retry")
    assert signature_hash(base) == signature_hash(other), (
        f"{label} changed the signature hash; callers would be needlessly invalidated"
    )


@pytest.mark.parametrize("label,mutated", SIGNATURE_CHANGED, ids=[c[0] for c in SIGNATURE_CHANGED])
def test_signature_changes_on_interface_edits(label: str, mutated: bytes) -> None:
    base = _sym(PY_BASE, "python", "should_retry")
    other = _sym(mutated, "python", "should_retry")
    assert signature_hash(base) != signature_hash(other), (
        f"{label} left the signature hash unchanged; callers would miss a breaking change"
    )


# --------------------------------------------------------------------------
# Block-structure changes — found by code review, not by the original corpus.
#
# Indentation is not a leaf, so in Python the token stream cannot distinguish a
# statement inside a block from one after it. A token-only hash judged these
# "fresh" and re-served a stale section. Braced languages are safe by accident.
# --------------------------------------------------------------------------

BLOCK_CASES: list[tuple[str, bytes, bytes]] = [
    (
        "statement moved out of if",
        b"def f(x):\n    if x:\n        return 1\n        return 2\n",
        b"def f(x):\n    if x:\n        return 1\n    return 2\n",
    ),
    (
        "statement moved into while",
        b"def g(n):\n    total = 0\n    while n:\n        n -= 1\n    total += n\n",
        b"def g(n):\n    total = 0\n    while n:\n        n -= 1\n        total += n\n",
    ),
    (
        "statement moved out of try",
        b"def h():\n    try:\n        a = 1\n        b = 2\n    except E:\n        pass\n",
        b"def h():\n    try:\n        a = 1\n    except E:\n        pass\n    b = 2\n",
    ),
    (
        "statement moved into else",
        b"def i(x):\n    if x:\n        a = 1\n    else:\n        pass\n    b = 2\n",
        b"def i(x):\n    if x:\n        a = 1\n    else:\n        pass\n        b = 2\n",
    ),
    (
        "return moved into for body",
        b"def j(xs):\n    for x in xs:\n        print(x)\n    return x\n",
        b"def j(xs):\n    for x in xs:\n        print(x)\n        return x\n",
    ),
]


@pytest.mark.parametrize("label,before,after", BLOCK_CASES, ids=[c[0] for c in BLOCK_CASES])
def test_block_boundary_changes_are_detected(label: str, before: bytes, after: bytes) -> None:
    """Moving a statement across a block boundary changes behaviour and must fire."""
    a = extract_symbols(before, "python")[0]
    b = extract_symbols(after, "python")[0]
    assert text_hash(a) != text_hash(b), (
        f"{label}: identical text_hash for a control-flow change; a stale section "
        "would be re-served as fresh"
    )


def test_block_fix_did_not_reintroduce_the_comment_false_positive() -> None:
    """The structure component omits comments, so this must still not fire."""
    base = _sym(PY_BASE, "python", "should_retry")
    commented = _sym(
        PY_BASE.replace(b"        delay = 3", b"        # tuned in PR#412\n        delay = 3"),
        "python",
        "should_retry",
    )
    assert text_hash(base) == text_hash(commented)
