"""Tests for the research loop.

Two properties here are load-bearing for modules elsewhere:

`test_the_model_never_authors_an_anchor_hash` guards the assumption
`doc/validate.py` makes when it treats a matching `text_hash` at a different ref
as a relocation rather than an invention. That reasoning is only sound because a
model never sees or writes a hash. If a draft could carry one, a decision made
two modules away silently becomes wrong.

`test_a_failed_section_does_not_discard_the_finished_ones` guards the reason the
section — not the document — is the unit of work.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kreb.budget.ledger import Ledger
from kreb.budget.policy import Budget
from kreb.doc.gate_a import run as gate_a
from kreb.index.repo_index import build_index
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Completion, Usage
from kreb.repo.access import Repository
from kreb.research.context import TRUNCATION_MARKER, build_pack
from kreb.research.draft import materialize, parse_draft
from kreb.research.loop import PlannedSection, run_research
from kreb.store.store import ArtifactStore

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

OTHER = "def unrelated():\n    return 'nothing to do with retries'\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_ENV)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "retry.py").write_text(SOURCE)
    (root / "other.py").write_text(OTHER)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return Repository(root)


@pytest.fixture()
def index(repo):
    return build_index(repo)


class ScriptedProvider:
    """Returns a scripted sequence of raw model outputs."""

    def __init__(self, *outputs, cost=0.1):
        self.outputs = list(outputs)
        self.cost = cost
        self.calls = 0
        self.prompts = []

    def model_for(self, role):
        return "test/model"

    def complete(self, request):
        self.calls += 1
        self.prompts.append(request.messages[-1].content)
        text = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        return Completion(
            text=text,
            usage=Usage(prompt_tokens=100, completion_tokens=50, cost=self.cost),
            model="test/model",
        )


def _draft(body="Some prose about retries.", cites=(), confidence="derived", evidence=()):
    return json.dumps(
        {
            "body": body,
            "cites": list(cites),
            "confidence": confidence,
            "evidence": [dict(e) for e in evidence],
        }
    )


def _metered(provider, *, budget=None, ledger=None):
    return MeteredProvider(
        inner=provider, ledger=ledger or Ledger(), budget=budget or Budget()
    )


# -- the invariant other modules depend on ---------------------------------


def test_the_model_never_authors_an_anchor_hash(index):
    """A draft carrying its own hash must be rejected outright.

    `doc/validate.py` treats a matching text_hash at a different ref as a
    relocation, not a fabrication, on the grounds that a model cannot produce
    one. Accepting a model-supplied hash would quietly break that.
    """
    with pytest.raises(Exception):
        parse_draft(
            json.dumps(
                {
                    "body": "x",
                    "cites": ["retry.py#backoff_seconds"],
                    "text_hash": "attacker-chosen",
                }
            )
        )


def test_hashes_come_from_the_index(index):
    draft = parse_draft(_draft(cites=["retry.py#backoff_seconds"]))
    result = materialize(
        draft, section_id="s1", title="t", kind="structure", index=index
    )
    symbol = index.symbols["retry.py#backoff_seconds"]
    anchor = result.section.anchors[0]
    assert anchor.text_hash == symbol.text_hash
    assert anchor.lines == (symbol.start_line, symbol.end_line)


def test_a_fabricated_citation_is_rejected_not_anchored(index):
    draft = parse_draft(_draft(cites=["retry.py#exponential_jitter"]))
    result = materialize(draft, section_id="s1", title="t", kind="structure", index=index)
    assert result.section.anchors == ()
    assert "fabricated" in result.rejections[0]


def test_duplicate_citations_collapse(index):
    draft = parse_draft(_draft(cites=["retry.py#backoff_seconds"] * 3))
    result = materialize(draft, section_id="s1", title="t", kind="structure", index=index)
    assert len(result.section.anchors) == 1


def test_verified_without_a_citation_is_downgraded_not_failed(index):
    """Regenerating to say the same thing more modestly is a wasted call."""
    draft = parse_draft(_draft(confidence="verified"))
    result = materialize(draft, section_id="s1", title="t", kind="structure", index=index)
    assert result.section.confidence == "derived"
    assert "downgraded" in result.rejections[0]


def test_secrets_in_generated_prose_are_scrubbed(index):
    draft = parse_draft(_draft(body='Configured via `api_key = "a1b2c3d4e5f6g7h8i9j0"`.'))
    result = materialize(draft, section_id="s1", title="t", kind="structure", index=index)
    assert "a1b2c3d4e5f6g7h8i9j0" not in result.section.body
    assert "REDACTED" in result.section.body


@pytest.mark.parametrize(
    "text",
    ["", "not json at all", "[1, 2, 3]", '{"body": 5}', "```json\n{bad}\n```"],
)
def test_malformed_output_is_rejected_rather_than_repaired(text):
    with pytest.raises(Exception):
        parse_draft(text)


def test_a_fenced_json_object_is_accepted():
    draft = parse_draft('```json\n{"body": "hello", "cites": []}\n```')
    assert draft.body == "hello"


# -- the writer's retry loop -----------------------------------------------


def test_a_bad_citation_triggers_a_retry_and_both_attempts_are_charged(index, repo):
    ledger = Ledger()
    provider = _metered(
        ScriptedProvider(
            _draft(cites=["retry.py#does_not_exist"]),
            _draft(cites=["retry.py#backoff_seconds"]),
        ),
        ledger=ledger,
    )
    report = run_research(
        plan=[PlannedSection(id="s1", title="Retries", kind="structure", refs=["retry.py#backoff_seconds"])],
        question="How do retries work?",
        index=index,
        repo=repo,
        provider=provider,
    )
    assert report.written == ["s1"]
    assert len(ledger.rows) == 2
    assert [r.failed for r in ledger.rows] == [True, False]


def test_the_retry_prompt_states_the_mechanical_reason(index, repo):
    inner = ScriptedProvider(
        _draft(cites=["retry.py#nope"]), _draft(cites=["retry.py#backoff_seconds"])
    )
    run_research(
        plan=[PlannedSection(id="s1", title="t", kind="structure", refs=["retry.py#backoff_seconds"])],
        question="q",
        index=index,
        repo=repo,
        provider=_metered(inner),
    )
    retry_prompt = inner.prompts[1]
    assert "previous attempt was rejected" in retry_prompt
    assert "retry.py#nope" in retry_prompt


def test_background_sections_may_not_cite_the_repository(index, repo):
    inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    report = run_research(
        plan=[PlannedSection(id="b1", title="About retries generally", kind="background", refs=[])],
        question="q",
        index=index,
        repo=repo,
        provider=_metered(inner),
    )
    assert "b1" in report.failed
    assert "must not cite repository symbols" in report.failed["b1"][0]


def test_a_section_that_never_validates_is_reported_not_shipped(index, repo):
    report = run_research(
        plan=[PlannedSection(id="s1", title="t", kind="structure", refs=[])],
        question="q",
        index=index,
        repo=repo,
        provider=_metered(ScriptedProvider(_draft(cites=["retry.py#ghost"]))),
    )
    assert report.document.sections == ()
    assert report.failed["s1"]
    assert report.complete is False


# -- the section is the unit ----------------------------------------------


def test_a_failed_section_does_not_discard_the_finished_ones(index, repo):
    """Section 39 failing must not throw away sections 1-38."""
    inner = ScriptedProvider(
        _draft(body="Good first section.", cites=["retry.py#backoff_seconds"]),
        _draft(cites=["retry.py#ghost"]),
        _draft(cites=["retry.py#ghost"]),
        _draft(cites=["retry.py#ghost"]),
        _draft(body="Good third section.", cites=["retry.py#RetryPolicy"]),
    )
    report = run_research(
        plan=[
            PlannedSection(id="a", title="A", kind="structure", refs=["retry.py#backoff_seconds"]),
            PlannedSection(id="b", title="B", kind="structure", refs=["retry.py#RetryPolicy"]),
            PlannedSection(id="c", title="C", kind="structure", refs=["retry.py#RetryPolicy"]),
        ],
        question="q",
        index=index,
        repo=repo,
        provider=_metered(inner),
    )
    assert [s.id for s in report.document.sections] == ["a", "c"]
    assert "b" in report.failed


def test_the_budget_stops_between_sections_and_names_what_was_skipped(index, repo):
    """A quietly shorter document is indistinguishable from a complete one."""
    ledger = Ledger()
    provider = _metered(
        ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]), cost=1.0),
        budget=Budget(max_per_run=1.5),
        ledger=ledger,
    )
    plan = [
        PlannedSection(id=f"s{i}", title=f"S{i}", kind="structure", refs=["retry.py#backoff_seconds"])
        for i in range(5)
    ]
    report = run_research(
        plan=plan, question="q", index=index, repo=repo, provider=provider
    )

    assert report.stopped_early is True
    assert "resumed" in report.stop_reason
    assert len(report.written) == 2  # 1.0 then 2.0 >= 1.5
    assert report.skipped == ["s2", "s3", "s4"]
    # The partial document is still a valid document.
    assert len(report.document.sections) == 2


def test_partial_documents_are_written_after_every_section(index, repo):
    inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    report = run_research(
        plan=[
            PlannedSection(id="a", title="A", kind="structure", refs=["retry.py#backoff_seconds"]),
            PlannedSection(id="b", title="B", kind="structure", refs=["retry.py#backoff_seconds"]),
        ],
        question="q",
        index=index,
        repo=repo,
        provider=_metered(inner),
    )
    assert len(report.document.sections) == 2
    from kreb.doc.schema import Document

    assert Document.from_json(report.document.to_json())


# -- caching ---------------------------------------------------------------


def test_a_second_run_reuses_sections_without_calling_the_model(index, repo, tmp_path):
    store = ArtifactStore(tmp_path / ".kreb")
    plan = [PlannedSection(id="s1", title="t", kind="structure", refs=["retry.py#backoff_seconds"])]

    first_inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    run_research(plan=plan, question="q", index=index, repo=repo,
                 provider=_metered(first_inner), store=store)
    assert first_inner.calls == 1

    second_inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    ledger = Ledger()
    report = run_research(plan=plan, question="q", index=index, repo=repo,
                          provider=_metered(second_inner, ledger=ledger), store=store)

    assert second_inner.calls == 0
    assert report.reused == ["s1"]
    assert report.cost == 0.0
    # Reuse is a zero-cost row, not an absent one.
    assert len(ledger.rows) == 1 and ledger.rows[0].cached is True


def test_an_unrelated_edit_does_not_invalidate_a_section(index, repo, tmp_path):
    """Editing a file the section never read must not regenerate it."""
    store = ArtifactStore(tmp_path / ".kreb")
    plan = [PlannedSection(id="s1", title="t", kind="structure", refs=["retry.py#backoff_seconds"])]
    run_research(plan=plan, question="q", index=index, repo=repo,
                 provider=_metered(ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))),
                 store=store)

    (repo.root / "other.py").write_text("def unrelated():\n    return 'changed entirely'\n")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-q", "-m", "unrelated change")
    after = build_index(Repository(repo.root))

    inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    report = run_research(plan=plan, question="q", index=after, repo=Repository(repo.root),
                          provider=_metered(inner), store=store)
    assert inner.calls == 0
    assert report.reused == ["s1"]


def test_editing_a_symbol_the_section_read_regenerates_it(index, repo, tmp_path):
    store = ArtifactStore(tmp_path / ".kreb")
    plan = [PlannedSection(id="s1", title="t", kind="structure", refs=["retry.py#backoff_seconds"])]
    run_research(plan=plan, question="q", index=index, repo=repo,
                 provider=_metered(ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))),
                 store=store)

    (repo.root / "retry.py").write_text(SOURCE.replace("min(2 ** attempt, 60)", "min(3 ** attempt, 90)"))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-q", "-m", "change backoff")
    after = build_index(Repository(repo.root))

    inner = ScriptedProvider(_draft(cites=["retry.py#backoff_seconds"]))
    report = run_research(plan=plan, question="q", index=after, repo=Repository(repo.root),
                          provider=_metered(inner), store=store)
    assert inner.calls == 1
    assert report.written == ["s1"]


# -- the context pack ------------------------------------------------------


def test_the_pack_carries_real_source(index, repo):
    pack = build_pack(question="q", refs=["retry.py#backoff_seconds"], index=index, repo=repo)
    assert "min(2 ** attempt, 60)" in pack.render()
    assert pack.refs == ["retry.py#backoff_seconds"]


def test_symbols_that_did_not_fit_are_named_not_dropped_silently(index, repo):
    """A model cannot hedge about a gap it was never told existed."""
    pack = build_pack(
        question="q",
        refs=["retry.py#RetryPolicy", "retry.py#backoff_seconds"],
        index=index,
        repo=repo,
        max_tokens=20,
    )
    assert len(pack.excerpts) == 1
    assert "must not be described as if read" in pack.render()
    assert "retry.py#backoff_seconds" in pack.render()


def test_a_truncated_symbol_says_so(index, repo, tmp_path):
    from kreb.research.context import excerpt_for

    excerpt = excerpt_for(index.symbols["retry.py#RetryPolicy"], repo, max_chars=40)
    assert excerpt.truncated is True
    assert TRUNCATION_MARKER.strip() in excerpt.source


def test_unknown_refs_are_reported(index, repo):
    pack = build_pack(question="q", refs=["retry.py#ghost"], index=index, repo=repo)
    assert pack.excerpts == []
    assert "not in the index" in pack.render()


def test_secrets_never_reach_the_prompt(tmp_path):
    root = tmp_path / "leaky"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "cfg.py").write_text('def connect():\n    api_key = "a1b2c3d4e5f6g7h8i9j0"\n    return api_key\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    leaky = Repository(root)
    leaky_index = build_index(leaky)

    pack = build_pack(question="q", refs=["cfg.py#connect"], index=leaky_index, repo=leaky)
    assert "a1b2c3d4e5f6g7h8i9j0" not in pack.render()
    assert "REDACTED" in pack.render()


# -- end to end ------------------------------------------------------------


def test_a_produced_document_passes_gate_a(index, repo):
    """The whole point: what the loop emits must clear the mechanical floor."""
    inner = ScriptedProvider(
        _draft(
            body="`backoff_seconds` caps the delay at 60 seconds regardless of attempt.",
            cites=["retry.py#backoff_seconds"],
            confidence="verified",
        )
    )
    report = run_research(
        plan=[PlannedSection(id="s1", title="Backoff", kind="structure",
                             refs=["retry.py#backoff_seconds"])],
        question="How does backoff work?",
        index=index,
        repo=repo,
        provider=_metered(inner),
    )
    result = gate_a(report.document, index)
    assert result.passed, result.summary()
    assert report.document.sections[0].confidence == "verified"
