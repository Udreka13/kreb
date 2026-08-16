"""The content-addressed artifact store.

Every artifact is written atomically (tmp + rename) the moment it validates, so
SIGINT or a budget stop never loses completed work — PRD §6.6 requires that a
run which hits its ceiling "stops cleanly and persists partial work", and that
promise is only true if nothing is held in memory until the end.

Every artifact carries a `provenance.json` sibling. For generated artifacts this
records the resolved provider slug as well as the model id: OpenRouter serves one
model id from several upstreams at different quantizations, so two artifacts with
the same key are otherwise not the same function.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kreb.store.keys import (
    SCHEMA_VERSION,
    DeterministicKey,
    GeneratedKey,
    Trace,
    TraceEntry,
)


@dataclass
class Provenance:
    """Everything needed to explain, or reproduce, one artifact."""

    kind: str
    key: str
    created_at: float = field(default_factory=time.time)
    # Generated artifacts only.
    model_id: str | None = None
    provider_slug: str | None = None
    prompt_hash: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    usage_cost: float | None = None
    response_id: str | None = None
    attempt: int = 0
    # Validation history — a section that passed only on a retry is a quality
    # signal worth surfacing, because laundering leaves a trace in this number.
    validation_attempts: int = 1
    trace: list[dict[str, str]] = field(default_factory=list)


class ArtifactStore:
    """Content-addressed storage under `.kreb/`.

    Layout::

        .kreb/v<schema>/<kind>/<key[:2]>/<key>/
            artifact          the bytes
            provenance.json   how it came to exist

    The two-character shard keeps directory sizes sane on repos that generate
    thousands of section artifacts.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.base = self.root / f"v{SCHEMA_VERSION}"
        # Counts recomputation, so tests can assert cache behaviour directly.
        self.stats = {"hits": 0, "misses": 0, "writes": 0, "invalidations": 0}

    # -- paths ------------------------------------------------------------

    def _dir(self, kind: str, key: str) -> Path:
        return self.base / kind / key[:2] / key

    def _artifact_path(self, kind: str, key: str) -> Path:
        return self._dir(kind, key) / "artifact"

    def _provenance_path(self, kind: str, key: str) -> Path:
        return self._dir(kind, key) / "provenance.json"

    # -- primitive read/write ---------------------------------------------

    def _write_atomic(self, path: Path, data: bytes) -> None:
        """Write via tmp+rename so a crash can never leave a partial artifact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(
        self,
        kind: str,
        key: str,
        data: bytes,
        provenance: Provenance,
    ) -> Path:
        path = self._artifact_path(kind, key)
        self._write_atomic(path, data)
        self._write_atomic(
            self._provenance_path(kind, key),
            json.dumps(asdict(provenance), indent=2, sort_keys=True).encode(),
        )
        self.stats["writes"] += 1
        return path

    def get(self, kind: str, key: str) -> bytes | None:
        path = self._artifact_path(kind, key)
        if not path.exists():
            return None
        return path.read_bytes()

    def provenance(self, kind: str, key: str) -> Provenance | None:
        path = self._provenance_path(kind, key)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        return Provenance(**raw)

    def exists(self, kind: str, key: str) -> bool:
        return self._artifact_path(kind, key).exists()

    # -- deterministic nodes ----------------------------------------------

    def materialize(self, key: DeterministicKey, compute) -> bytes:
        """Return the artifact for `key`, computing it only on a miss.

        `compute` is called with no arguments and must return bytes. Because a
        deterministic node is a pure function of its declared inputs, a hit is
        always safe — no validity check is needed or possible.
        """
        digest = key.digest()
        cached = self.get(key.kind, digest)
        if cached is not None:
            self.stats["hits"] += 1
            return cached

        self.stats["misses"] += 1
        data = compute()
        self.put(
            key.kind,
            digest,
            data,
            Provenance(kind=key.kind, key=digest, params=dict(key.params)),
        )
        return data

    # -- generated nodes ---------------------------------------------------

    def materialize_generated(
        self,
        key: GeneratedKey,
        current_hashes: dict[str, str],
        generate,
    ) -> tuple[bytes, bool]:
        """Return the artifact for `key`, regenerating if its trace went stale.

        `generate` returns `(data, trace, provenance)`. Returns the artifact and
        whether it came from cache.

        This is where identity and validity separate: a matching key is not
        sufficient — the recorded trace must still hold. Equally, a *changed
        repo* is not sufficient to invalidate: only a change to something this
        node actually read.
        """
        digest = key.digest()
        cached = self.get(key.kind, digest)
        if cached is not None:
            prov = self.provenance(key.kind, digest)
            trace = Trace(
                entries=tuple(
                    TraceEntry(ref=e["ref"], text_hash=e["text_hash"])
                    for e in (prov.trace if prov else [])
                )
            )
            if trace.is_valid(current_hashes):
                self.stats["hits"] += 1
                return cached, True
            self.stats["invalidations"] += 1

        self.stats["misses"] += 1
        data, trace, provenance = generate()
        provenance.kind = key.kind
        provenance.key = digest
        provenance.attempt = key.attempt
        provenance.trace = [asdict(e) for e in trace.entries]
        self.put(key.kind, digest, data, provenance)
        return data, False

    def invalid_refs(self, key: GeneratedKey, current_hashes: dict[str, str]) -> list[str]:
        """Which consulted refs have changed since this artifact was written."""
        prov = self.provenance(key.kind, key.digest())
        if prov is None:
            return []
        trace = Trace(
            entries=tuple(
                TraceEntry(ref=e["ref"], text_hash=e["text_hash"]) for e in prov.trace
            )
        )
        return trace.stale_refs(current_hashes)
