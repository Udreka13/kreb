"""Tests for the document schema, validators and Gate A.

Every rule gets a document that a careless pipeline would plausibly produce: a
real symbol at the wrong path, an invented function name, a `verified` claim
with nothing behind it, library documentation dressed up as codebase knowledge.
Passing these is not evidence the output is *useful* — that is Gate B, and it is
human. These only establish that the mechanical floor holds.
"""

from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from kreb.doc import gate_a, validate
from kreb.doc.gate_a import staleness_recall
from kreb.doc.schema import (
    Anchor,
    Capabilities,
    DiagramSpec,
    Document,
    Evidence,
    Section,
)
from kreb.doc.scrub import contains_secret, redact
from kreb.doc.validate import anchor_staleness, identifiers_in
from kreb.index.repo_index import build_index
from kreb.repo.access import Repository

_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

SOURCE = '''\
MAX_RETRIES = 5


class RetryPolicy:
    """Decides whether another attempt is worth making."""

    def should_retry(self, attempt, error):
        if attempt >= MAX_RETRIES:
            return False
        return isinstance(error, TimeoutError)


def backoff_seconds(attempt):
    return min(2 ** attempt, 60)
'''


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_ENV)


@pytest.fixture()
def index(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "retry.py").write_text(SOURCE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return build_index(Repository(root))


@pytest.fixture()
def caps(index):
    return Capabilities(base_sha=index.sha, git="ok", forge="anonymous", languages=("python",))


def _anchor(index, ref):
    return Anchor(ref=ref, text_hash=index.symbols[ref].text_hash)


def _doc(caps, *sections):
    return Document(title="t", capabilities=caps, sections=sections)


# -- schema ----------------------------------------------------------------


def test_document_round_trips_through_json(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="Retries",
            kind="structure",
            body="`RetryPolicy.should_retry` gives up after `MAX_RETRIES`.",
            confidence="verified",
            anchors=(_anchor(index, "retry.py#RetryPolicy.should_retry"),),
            evidence=(Evidence(kind="symbol", ref="retry.py#RetryPolicy.should_retry"),),
        ),
    )
    assert Document.from_json(doc.to_json()) == doc


def test_schema_version_mismatch_is_refused_not_coerced():
    """Reading an old document as if it matched is how silent corruption starts."""
    doc = Document(title="t", capabilities=Capabilities(base_sha="abc"))
    text = doc.to_json().replace('"schema_version": 1', '"schema_version": 99')
    with pytest.raises(ValueError, match="schema version"):
        Document.from_json(text)


def test_unknown_fields_are_rejected(caps):
    """Model output is the untrusted input here; a typo'd key must not vanish."""
    doc = _doc(caps)
    text = doc.to_json().replace('"title"', '"titel"', 1)
    with pytest.raises(ValidationError):
        Document.from_json(text)


def test_duplicate_section_ids_are_rejected(caps):
    section = Section(id="s1", title="a", kind="structure", body="b")
    with pytest.raises(ValidationError, match="duplicate section id"):
        _doc(caps, section, section)


@pytest.mark.parametrize("ref", ["nohash", "#leading", "trailing#"])
def test_malformed_anchor_refs_are_rejected(ref):
    with pytest.raises(ValidationError):
        Anchor(ref=ref, text_hash="x")


# -- anchor rules ----------------------------------------------------------


def test_resolving_anchor_passes(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(_anchor(index, "retry.py#backoff_seconds"),),
        ),
    )
    assert validate(doc, index).ok


def test_fabricated_anchor_is_a_hard_fail(index, caps):
    """A symbol that exists nowhere. The worst case: it looks like evidence."""
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(Anchor(ref="retry.py#exponential_jitter", text_hash="deadbeef"),),
        ),
    )
    report = validate(doc, index)
    assert not report.ok
    assert [f.rule for f in report.failures] == ["fabricated_anchor"]


def test_misplaced_anchor_is_reported_separately_from_fabricated(index, caps):
    """A real symbol at the wrong path passes one rule and fails the other.

    Collapsing these into "bad anchor" throws away the diagnosis: one means the
    path went stale, the other means the name was invented.
    """
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(Anchor(ref="http.py#backoff_seconds", text_hash="not-the-real-hash"),),
        ),
    )
    report = validate(doc, index)
    rules = [f.rule for f in report.failures]
    assert "misplaced_anchor" in rules
    assert "fabricated_anchor" not in rules
    assert "retry.py#backoff_seconds" in report.failures[0].message


def test_matching_content_elsewhere_reads_as_moved_not_misplaced(index, caps):
    """Precedence decision, locked in deliberately.

    A wrong path whose recorded content still exists at another ref is a
    relocation, not an invention — because `text_hash` is filled from the index
    by the pipeline, never authored by the model, so a matching hash cannot be
    fabricated. Hard-failing a real `git mv` would be wrong and user-hostile.
    """
    real_hash = index.symbols["retry.py#backoff_seconds"].text_hash
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(Anchor(ref="http.py#backoff_seconds", text_hash=real_hash),),
        ),
    )
    report = validate(doc, index)
    assert report.ok
    assert [f.rule for f in report.warnings] == ["moved_anchor"]


def test_moved_symbol_is_not_treated_as_a_dangling_anchor(index, caps, tmp_path):
    """A relocated function means the address changed, not that the claim was wrong."""
    root = tmp_path / "moved"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "policy.py").write_text(SOURCE)  # same content, different file
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "move")
    after = build_index(Repository(root))

    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(_anchor(index, "retry.py#backoff_seconds"),),
        ),
    )
    report = validate(doc, after)
    assert report.ok, [str(f) for f in report.failures]
    assert report.staleness["retry.py#backoff_seconds"] == "moved"
    assert report.moved["retry.py#backoff_seconds"] == "policy.py#backoff_seconds"


def test_changed_symbol_is_flagged_stale(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="structure",
            body="text",
            anchors=(Anchor(ref="retry.py#backoff_seconds", text_hash="stale-hash"),),
        ),
    )
    report = validate(doc, index)
    assert report.staleness["retry.py#backoff_seconds"] == "stale"
    assert [f.rule for f in report.warnings] == ["stale_anchor"]
    assert report.ok  # stale is a state to report, not a validation failure


# -- confidence ------------------------------------------------------------


def test_verified_without_an_anchor_is_a_hard_fail(index, caps):
    """`verified` must be earned. If it can mean "the model felt sure", the tier
    carries no information and the reader is better off without it."""
    doc = _doc(
        caps,
        Section(id="s1", title="a", kind="rationale", body="It works this way.",
                confidence="verified"),
    )
    report = validate(doc, index)
    assert [f.rule for f in report.failures] == ["verified_without_anchor"]


def test_speculative_without_an_anchor_is_fine(index, caps):
    doc = _doc(
        caps,
        Section(id="s1", title="a", kind="rationale", body="Possibly historical.",
                confidence="speculative"),
    )
    assert validate(doc, index).ok


# -- the replacement for the verb linter -----------------------------------


def test_repo_symbols_named_without_repo_evidence_is_a_hard_fail(index, caps):
    """Library docs restated as this repo's behaviour, caught structurally.

    The check is positive — the section must *carry* evidence — because the
    negative semantic version is unenforceable and, behind a retry loop, worse
    than nothing: the model rewrites until it passes and the claim is laundered.
    """
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="Retries",
            kind="background",
            body="`RetryPolicy` retries three times by default.",
            evidence=(Evidence(kind="external", ref="https://docs.example/retry"),),
        ),
    )
    report = validate(doc, index)
    assert "external_only_repo_claim" in [f.rule for f in report.failures]


def test_the_same_claim_with_repo_evidence_passes(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="Retries",
            kind="structure",
            body="`RetryPolicy` gives up after `MAX_RETRIES`.",
            anchors=(_anchor(index, "retry.py#RetryPolicy"),),
            evidence=(
                Evidence(kind="symbol", ref="retry.py#RetryPolicy"),
                Evidence(kind="external", ref="https://docs.example/retry"),
            ),
        ),
    )
    assert validate(doc, index).ok


def test_background_naming_no_repo_symbols_is_untouched(index, caps):
    """The desired background section: about the world, evidence external only."""
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="About urllib",
            kind="background",
            body="`urlopen` accepts a timeout in seconds.",
            evidence=(Evidence(kind="external", ref="https://docs.python.org/"),),
        ),
    )
    assert validate(doc, index).ok


def test_background_carrying_repo_anchors_warns_but_does_not_fail(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="background",
            body="General notes.",
            anchors=(_anchor(index, "retry.py#RetryPolicy"),),
        ),
    )
    report = validate(doc, index)
    assert report.ok
    assert [f.rule for f in report.warnings] == ["background_cites_repo"]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("`RetryPolicy` is here", {"RetryPolicy"}),
        ("call `backoff_seconds(3)` now", {"backoff_seconds"}),
        ("RetryPolicy without backticks", {"RetryPolicy"}),
        ("`obj.should_retry` chained", {"should_retry"}),
        ("no identifiers at all here", set()),
    ],
)
def test_identifier_detection(body, expected, index):
    from kreb.doc.validate import indexed_identifiers

    assert indexed_identifiers(body, index) == expected


# -- manifest consistency --------------------------------------------------


def test_commit_evidence_contradicting_a_shallow_manifest_fails(index):
    caps = Capabilities(base_sha=index.sha, git="shallow")
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="rationale",
            body="text",
            evidence=(Evidence(kind="commit", ref="a" * 40),),
        ),
    )
    report = validate(doc, index)
    assert [f.rule for f in report.failures] == ["impossible_evidence"]


def test_forge_evidence_without_forge_access_fails(index):
    caps = Capabilities(base_sha=index.sha, forge="none")
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="a",
            kind="rationale",
            body="text",
            evidence=(Evidence(kind="pull_request", ref="42"),),
        ),
    )
    assert "impossible_evidence" in [f.rule for f in validate(doc, index).failures]


def test_capability_warnings_name_every_degradation(index):
    caps = Capabilities(
        base_sha=index.sha, git="shallow", forge="rate_limited", dirty=True,
        degraded_files=12, total_files=100,
    )
    text = " ".join(caps.warnings()).lower()
    assert "shallow" in text and "rate limit" in text and "dirty" in text and "12" in text


# -- diagrams --------------------------------------------------------------


def test_extracted_diagram_without_anchors_fails(index, caps):
    """An extracted and an asserted diagram look identical on screen."""
    doc = _doc(
        caps,
        Section(
            id="s1", title="a", kind="structure", body="text",
            diagram=DiagramSpec(title="d", d2_source="a -> b", provenance="extracted"),
        ),
    )
    assert "unanchored_extracted_diagram" in [f.rule for f in validate(doc, index).failures]


def test_asserted_diagram_without_anchors_is_fine(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1", title="a", kind="structure", body="text",
            diagram=DiagramSpec(title="d", d2_source="a -> b", provenance="asserted"),
        ),
    )
    assert validate(doc, index).ok


# -- secrets ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIA1234567890ABCDEF",
        "ghp_" + "a1b2c3d4e5" * 4,
        "sk-ant-api03-" + "x7y8z9" * 6,
        'api_key = "a1b2c3d4e5f6g7h8i9j0"',
    ],
)
def test_credentials_are_detected(text):
    assert contains_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "api_key = get_api_key()",
        'api_key = "your-key-here"',
        'password = "<redacted>"',
        'token = "${GITHUB_TOKEN}"',
        "the sk- prefix identifies OpenAI keys",
        "self.password = password",
        # AWS's own documentation key. Real repos quote it constantly; firing on
        # it would mean firing on half the READMEs that mention S3.
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_ordinary_code_is_not_flagged(text):
    """A noisy detector gets switched off, and then the real leak goes through."""
    assert not contains_secret(text)


def test_redaction_preserves_surrounding_code():
    text = 'client = Client(api_key="a1b2c3d4e5f6g7h8i9j0")\nreturn client'
    out = redact(text)
    assert "a1b2c3d4e5f6g7h8i9j0" not in out
    assert "client = Client(" in out and "return client" in out


def test_secret_in_a_section_body_is_a_hard_fail(index, caps):
    doc = _doc(
        caps,
        Section(id="s1", title="a", kind="structure",
                body="Configured as `api_key = \"a1b2c3d4e5f6g7h8i9j0\"`."),
    )
    assert "secret_in_body" in [f.rule for f in validate(doc, index).failures]


# -- Gate A ----------------------------------------------------------------


def test_gate_a_passes_a_clean_document(index, caps):
    doc = _doc(
        caps,
        Section(
            id="s1",
            title="Retries",
            kind="structure",
            body="`RetryPolicy.should_retry` stops after `MAX_RETRIES` attempts.",
            confidence="verified",
            anchors=(_anchor(index, "retry.py#RetryPolicy.should_retry"),),
            evidence=(Evidence(kind="symbol", ref="retry.py#RetryPolicy.should_retry"),),
        ),
    )
    result = gate_a(doc, index)
    assert result.passed, result.summary()


def test_gate_a_fails_and_names_the_rule(index, caps):
    doc = _doc(
        caps,
        Section(id="s1", title="a", kind="structure", body="text",
                anchors=(Anchor(ref="retry.py#nope", text_hash="x"),)),
    )
    result = gate_a(doc, index)
    assert not result.passed
    assert "fabricated anchors" in result.summary()


def test_gate_a_always_reports_what_it_cannot_check(index, caps):
    """A gate that reports 4/4 green while skipping the hard three is worse
    than no gate: it manufactures the assurance it fails to provide."""
    summary = gate_a(_doc(caps), index).summary()
    assert "Not mechanically decidable" in summary
    assert "actually inferred" in summary
    assert "pinned version" in summary


def test_staleness_recall_catches_a_synthetic_refactor(index, caps, tmp_path):
    """The original fatal bug, measured as recall against a known-changed set."""
    root = tmp_path / "after"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "retry.py").write_text(SOURCE.replace("min(2 ** attempt, 60)", "min(3 ** attempt, 90)"))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "refactor")
    after = build_index(Repository(root))

    doc = _doc(
        caps,
        Section(id="s1", title="a", kind="structure", body="text",
                anchors=(_anchor(index, "retry.py#backoff_seconds"),)),
    )
    check = staleness_recall(doc, index, after)
    assert check.passed
    assert check.measured == "1/1"
    assert anchor_staleness(doc.sections[0].anchors[0], after)[0] == "stale"


def test_identifiers_in_finds_backticked_and_conventional_tokens():
    found = identifiers_in("`RetryPolicy` and backoff_seconds and CamelCase")
    assert {"RetryPolicy", "backoff_seconds", "CamelCase"} <= found
