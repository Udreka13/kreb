"""Detectors for FATAL bug #2 — the cache that invalidated everything.

The v0.1 design keyed the document on a whole-repository hash, so any file
change anywhere rekeyed it, invalidating the HTML, the narration, the audio and
the video. A README typo would re-bill a $2 document and re-render forty minutes
of TTS, against a PRD that promises re-renders are near-free.

Four properties. The first two prove the abstraction holds at all; the second
two are the bug stated as tests.

    idempotence      materialize twice -> zero recomputation the second time
    reproducibility  delete a deterministic node -> byte-identical on rebuild
    isolation        touch something nothing depends on -> nothing invalidates
    precision        change one symbol -> exactly its dependents invalidate

`precision` is the one to trust most: it fails in both directions, since
under-invalidation is a stale document and over-invalidation is the cost
explosion. See architecture.md §1 and §9.
"""

from __future__ import annotations

import pytest

from kreb.store.keys import DeterministicKey, GeneratedKey, Trace, TraceEntry
from kreb.store.store import ArtifactStore, Provenance


@pytest.fixture()
def store(tmp_path):
    return ArtifactStore(tmp_path / ".kreb")


def _det(kind: str, *inputs: str, **params) -> DeterministicKey:
    return DeterministicKey(kind=kind, inputs=inputs, params=params)


def _gen(node_id: str, *, model: str = "test/model", attempt: int = 0) -> GeneratedKey:
    return GeneratedKey(
        kind="section",
        node_id=node_id,
        brief="explain the retry policy",
        prompt_hash="prompt-v1",
        model_id=model,
        attempt=attempt,
    )


def _writer(payload: bytes, reads: dict[str, str], calls: list[str]):
    """A fake section writer that records having been called."""

    def generate():
        calls.append("called")
        trace = Trace(
            entries=tuple(TraceEntry(ref=r, text_hash=h) for r, h in reads.items())
        )
        return payload, trace, Provenance(kind="section", key="", model_id="test/model")

    return generate


# ---------------------------------------------------------------------------
# 1. Idempotence
# ---------------------------------------------------------------------------

def test_deterministic_node_is_computed_once(store):
    calls = []

    def compute():
        calls.append("called")
        return b"index-data"

    key = _det("index", "repo@abc123")
    assert store.materialize(key, compute) == b"index-data"
    assert store.materialize(key, compute) == b"index-data"

    assert calls == ["called"], "deterministic node recomputed on a cache hit"
    assert store.stats["hits"] == 1


def test_generated_node_makes_no_second_call_when_trace_holds(store):
    """The headline property: materializing twice makes zero provider calls."""
    calls: list[str] = []
    hashes = {"src/http.py#RetryPolicy": "h1"}
    key = _gen("retry-policy")

    data, cached = store.materialize_generated(
        key, hashes, _writer(b"section one", hashes, calls)
    )
    assert data == b"section one"
    assert cached is False

    data, cached = store.materialize_generated(
        key, hashes, _writer(b"section one", hashes, calls)
    )
    assert cached is True, "second materialization regenerated instead of hitting cache"
    assert len(calls) == 1, f"expected 1 provider call across two runs, got {len(calls)}"


# ---------------------------------------------------------------------------
# 2. Reproducibility
# ---------------------------------------------------------------------------

def test_deleted_deterministic_node_rebuilds_byte_identical(store):
    key = _det("map", "repo@abc123", depth=2)
    first = store.materialize(key, lambda: b"deterministic-output")

    path = store._artifact_path(key.kind, key.digest())
    path.unlink()

    second = store.materialize(key, lambda: b"deterministic-output")
    assert first == second, "rebuild was not byte-identical"


def test_params_participate_in_the_key(store):
    """Two different requests must not collide on one artifact."""
    a = store.materialize(_det("map", "repo@abc", depth=1), lambda: b"depth-1")
    b = store.materialize(_det("map", "repo@abc", depth=2), lambda: b"depth-2")
    assert a != b


def test_code_version_would_invalidate_deterministic_nodes():
    """Deterministic keys carry CODE_VERSION so logic changes invalidate."""
    from kreb.store import keys

    key = _det("index", "repo@abc")
    before = key.digest()
    original = keys.CODE_VERSION
    try:
        keys.CODE_VERSION = "999"
        after = key.digest()
    finally:
        keys.CODE_VERSION = original
    assert before != after, "CODE_VERSION does not participate in the key"


# ---------------------------------------------------------------------------
# 3. Isolation — THE BUG, stated as a test
# ---------------------------------------------------------------------------

def test_unrelated_change_invalidates_nothing(store):
    """A README typo must not invalidate a single section.

    This is precisely what v0.1 got wrong: the document was keyed on a
    whole-repo hash, so any edit anywhere rekeyed everything downstream.
    """
    calls: list[str] = []
    reads = {"src/http.py#RetryPolicy": "h1"}
    key = _gen("retry-policy")

    store.materialize_generated(key, reads, _writer(b"section", reads, calls))

    # The repository changed — a README, a new unrelated module, a reformat of
    # a file this section never consulted.
    world_after = {
        "src/http.py#RetryPolicy": "h1",  # unchanged: this is what we read
        "README.md#doc": "totally-different",
        "src/unrelated.py#Widget": "brand-new",
    }

    _, cached = store.materialize_generated(
        key, world_after, _writer(b"section", reads, calls)
    )
    assert cached is True, "an unrelated repo change invalidated the section"
    assert len(calls) == 1, "an unrelated repo change caused a regeneration"
    assert store.stats["invalidations"] == 0


# ---------------------------------------------------------------------------
# 4. Precision — fails in both directions
# ---------------------------------------------------------------------------

def test_consulted_symbol_change_invalidates_exactly_its_dependents(store):
    calls_a: list[str] = []
    calls_b: list[str] = []

    reads_a = {"src/http.py#RetryPolicy": "h1"}
    reads_b = {"src/db.py#Session": "h2"}

    key_a, key_b = _gen("retry-policy"), _gen("db-session")
    store.materialize_generated(key_a, reads_a, _writer(b"A", reads_a, calls_a))
    store.materialize_generated(key_b, reads_b, _writer(b"B", reads_b, calls_b))

    # RetryPolicy's body changed; Session did not.
    world = {"src/http.py#RetryPolicy": "CHANGED", "src/db.py#Session": "h2"}

    _, a_cached = store.materialize_generated(key_a, world, _writer(b"A2", reads_a, calls_a))
    _, b_cached = store.materialize_generated(key_b, world, _writer(b"B", reads_b, calls_b))

    assert a_cached is False, "under-invalidation: a stale section was served from cache"
    assert b_cached is True, "over-invalidation: an unaffected section was regenerated"
    assert len(calls_a) == 2
    assert len(calls_b) == 1


def test_vanished_symbol_invalidates(store):
    """A consulted symbol that no longer exists must invalidate, not silently pass."""
    calls: list[str] = []
    reads = {"src/http.py#RetryPolicy": "h1"}
    key = _gen("retry-policy")
    store.materialize_generated(key, reads, _writer(b"section", reads, calls))

    _, cached = store.materialize_generated(key, {}, _writer(b"section", reads, calls))
    assert cached is False, "a deleted symbol did not invalidate its section"


def test_stale_refs_reports_the_reason(store):
    reads = {"a#X": "h1", "b#Y": "h2"}
    key = _gen("multi")
    store.materialize_generated(key, reads, _writer(b"s", reads, []))

    changed = {"a#X": "h1", "b#Y": "DIFFERENT"}
    assert store.invalid_refs(key, changed) == ["b#Y"]


# ---------------------------------------------------------------------------
# 5. The silent-upgrade bug: prompt and schema versioning
# ---------------------------------------------------------------------------

def test_prompt_change_misses_cache(store):
    """Editing a prompt must invalidate generated nodes.

    Without this the classic LLM-pipeline bug appears: users upgrade via uvx,
    the prompt changes, and stale-but-plausible output is served from cache
    forever. Plausibility is exactly why it goes unnoticed.
    """
    reads = {"src/http.py#RetryPolicy": "h1"}
    calls: list[str] = []

    v1 = _gen("retry-policy")
    v2 = GeneratedKey(
        kind="section",
        node_id="retry-policy",
        brief="explain the retry policy",
        prompt_hash="prompt-v2",  # the only difference
        model_id="test/model",
    )
    assert v1.digest() != v2.digest(), "prompt_hash does not participate in the key"

    store.materialize_generated(v1, reads, _writer(b"old", reads, calls))
    _, cached = store.materialize_generated(v2, reads, _writer(b"new", reads, calls))
    assert cached is False
    assert len(calls) == 2


def test_model_change_misses_cache(store):
    a, b = _gen("s", model="cheap/model"), _gen("s", model="frontier/model")
    assert a.digest() != b.digest(), "model_id does not participate in the key"


def test_regen_attempt_produces_a_distinct_artifact(store):
    """`kreb regen` must not require deleting the cache.

    Nondeterminism plus caching means the first result wins forever; a bad
    section would be re-served identically on every rerun. Bumping `attempt`
    keeps both versions so they can be compared.
    """
    reads = {"a#X": "h1"}
    calls: list[str] = []
    first = _gen("retry-policy", attempt=0)
    retry = _gen("retry-policy", attempt=1)

    store.materialize_generated(first, reads, _writer(b"first try", reads, calls))
    data, cached = store.materialize_generated(retry, reads, _writer(b"second try", reads, calls))

    assert cached is False
    assert data == b"second try"
    assert store.get("section", first.digest()) == b"first try", "original was lost"


# ---------------------------------------------------------------------------
# 6. Durability
# ---------------------------------------------------------------------------

def test_writes_are_atomic_and_leave_no_temp_files(store):
    key = _det("index", "repo@abc")
    store.materialize(key, lambda: b"data")
    leftovers = list(store.base.rglob(".tmp-*"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_failed_compute_writes_nothing(store):
    key = _det("index", "repo@abc")

    def boom():
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError):
        store.materialize(key, boom)

    assert not store.exists(key.kind, key.digest()), "a failed node was persisted"


def test_provenance_records_how_it_was_made(store):
    reads = {"a#X": "h1"}

    def generate():
        return (
            b"payload",
            Trace(entries=(TraceEntry(ref="a#X", text_hash="h1"),)),
            Provenance(
                kind="section",
                key="",
                model_id="deepseek/deepseek-v4-pro",
                provider_slug="deepseek-official",
                usage_cost=0.0031,
                response_id="gen-abc123",
                validation_attempts=2,
            ),
        )

    key = _gen("retry-policy")
    store.materialize_generated(key, reads, generate)

    prov = store.provenance("section", key.digest())
    assert prov is not None
    assert prov.provider_slug == "deepseek-official", "resolved provider slug not recorded"
    assert prov.usage_cost == 0.0031
    assert prov.validation_attempts == 2, "retry count not recorded (laundering signal)"
    assert prov.trace == [{"ref": "a#X", "text_hash": "h1"}]
