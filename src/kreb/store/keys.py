"""Artifact keys — the distinction between *identity* and *validity*.

The v0.1 architecture keyed every node on a deep hash of its inputs, which is
correct for build systems with statically-known dependencies and wrong here. A
research node's input set is not knowable before it runs: the agent decides what
to read via tool calls, so a section's `depends_on` is an **output**, not an
input. Keying on the whole-repo hash therefore invalidated the entire product on
every commit — a README typo re-billed the document and re-rendered the audio.

So there are two node classes:

    DeterministicNode   key = hash(inputs ‖ params ‖ code_version)
                        Constructive trace. Byte-reproducible. Free to recompute.

    GeneratedNode       key = hash(node_id ‖ brief ‖ prompt ‖ model ‖ params ‖ schema)
                        Deliberately EXCLUDES repo state. Freshness comes from a
                        separate *verifying trace*: the (symbol, text_hash) pairs
                        the writer actually consulted.

`identity` answers "is this the same request?"; `validity` answers "is the answer
still true?". Conflating them is the bug. See architecture.md §1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Bumped when the on-disk artifact layout changes incompatibly. Lives in the
# store path, so a version bump cannot misread old artifacts.
SCHEMA_VERSION = "1"

# Bumped when deterministic node *logic* changes in a way that should
# invalidate previously-computed artifacts.
CODE_VERSION = "1"

_SEP = b"\x1f"


def _canonical(value: Any) -> bytes:
    """Stable serialization — sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(*parts: bytes) -> str:
    return hashlib.sha256(_SEP.join(parts)).hexdigest()


@dataclass(frozen=True)
class DeterministicKey:
    """Key for a node that is a pure function of declared inputs."""

    kind: str
    inputs: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        return _digest(
            self.kind.encode(),
            _canonical(sorted(self.inputs)),
            _canonical(self.params),
            CODE_VERSION.encode(),
        )


@dataclass(frozen=True)
class GeneratedKey:
    """Key for a node produced by a model.

    Note what is absent: repo state. Two runs against different commits produce
    the *same* key if the request is the same — freshness is then decided by the
    verifying trace, not by the key. That is what stops a README typo from
    invalidating every section.

    `attempt` exists so `kreb regen` can force a fresh generation without
    deleting the cache, keeping both versions for comparison. Without it the
    first result wins forever and a bad section is re-served identically on every
    rerun.
    """

    kind: str
    node_id: str
    brief: str
    prompt_hash: str
    model_id: str
    params: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0

    def digest(self) -> str:
        return _digest(
            self.kind.encode(),
            self.node_id.encode(),
            self.brief.encode(),
            self.prompt_hash.encode(),
            self.model_id.encode(),
            _canonical(self.params),
            str(self.attempt).encode(),
            SCHEMA_VERSION.encode(),
        )


@dataclass(frozen=True)
class TraceEntry:
    """One thing a generated node actually read, and what it hashed at the time."""

    ref: str
    text_hash: str


@dataclass(frozen=True)
class Trace:
    """The verifying trace: everything consulted while producing an artifact.

    A generated artifact is valid iff every entry still hashes equal in the
    current index. Empty traces are permitted but suspicious — a section that
    consulted nothing cannot make a `verified` claim.
    """

    entries: tuple[TraceEntry, ...] = ()

    def is_valid(self, current: dict[str, str]) -> bool:
        """True iff the trace is non-empty and every entry still hashes equal.

        A ref that has vanished counts as invalid: the symbol may have moved or
        been deleted, and either way the section must be re-examined.

        **An empty trace is invalid, not vacuously valid.** `all(())` is True,
        which would make a node that recorded nothing permanently fresh no
        matter how the repository changed — and that is reachable in practice,
        because a crash between writing an artifact and writing its provenance
        leaves exactly that state. A node that consulted nothing also cannot
        support a `verified` claim, so regenerating it is right on both counts.
        """
        if not self.entries:
            return False
        return all(current.get(e.ref) == e.text_hash for e in self.entries)

    def stale_refs(self, current: dict[str, str]) -> list[str]:
        """Which refs changed — the reason a node is invalid, for reporting."""
        return [e.ref for e in self.entries if current.get(e.ref) != e.text_hash]
