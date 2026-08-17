"""Tests for the Gate B worksheet.

The load-bearing property here is a negative one: `test_the_worksheet_judges_nothing`.
Gate B is defined as a human's verdict, and the one thing that would quietly
destroy it is this module acquiring an opinion — a heuristic novelty score, a
self-check on truth. Then the gate measures the pipeline against itself and
passes by construction. The test asserts the absence, because absence is exactly
what erodes without one.

Everything else here is about the *cost* of judging: a claim is only cheap to
check if the source it cites is sitting next to it, at the right lines.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kreb.cli import main
from kreb.doc.gate_b import (
    NOVEL_TRUE_REQUIRED,
    Claim,
    Worksheet,
    build,
    split_claims,
)
from kreb.doc.schema import Anchor, Capabilities, Document, Evidence, Section
from kreb.index.repo_index import build_index
from kreb.render.worksheet import render, render_markdown
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

SOURCE = """\
MAX_RETRIES = 5


def backoff_seconds(attempt):
    return min(2 ** attempt, 60)
"""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_ENV)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "retry.py").write_text(SOURCE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return Repository(root)


@pytest.fixture()
def index(repo):
    return build_index(repo)


def _doc(index, *sections):
    return Document(
        title="t",
        question="q",
        capabilities=Capabilities(base_sha=index.sha),
        sections=tuple(sections),
    )


def _section(index, body, *, confidence="verified", kind="structure", cite=True, **kw):
    symbol = index.symbols["retry.py#backoff_seconds"]
    anchors = (
        (Anchor(ref=symbol.ref, text_hash=symbol.text_hash, lines=(4, 5)),) if cite else ()
    )
    return Section(
        id=kw.pop("id", "s1"),
        title=kw.pop("title", "Backoff"),
        kind=kind,
        body=body,
        confidence=confidence,
        anchors=anchors,
        **kw,
    )


# -- the property that keeps the gate a gate --------------------------------


def test_the_worksheet_judges_nothing(index, repo):
    """Novelty lives in the reader's head; truth at `verified` is under test.

    A pipeline that grades its own output on the axis it is being tested for is
    a mirror, not a gate.
    """
    sheet = build(_doc(index, _section(index, "The delay is capped at 60 seconds.")), index, repo)
    forbidden = {"true", "novel", "correct", "wrong", "score", "verdict", "rating"}
    fields = set(Claim.__dataclass_fields__)
    assert not (fields & forbidden), f"a claim must carry no verdict, found {fields & forbidden}"
    assert not any(isinstance(v, bool) for v in vars(sheet.claims[0]).values())


def test_a_verdict_requires_counts_from_outside(index, repo):
    sheet = build(_doc(index, _section(index, "A claim.")), index, repo)
    assert sheet.score(novel_true=3, wrong_at_verified=0).passed is True
    assert sheet.score(novel_true=2, wrong_at_verified=0).passed is False
    assert sheet.score(novel_true=9, wrong_at_verified=1).passed is False


def test_one_wrong_verified_claim_fails_a_document_that_taught_a_lot():
    """The threshold is zero, and zero is the point: a document that is right
    forty times and confidently wrong once is not a document you can trust."""
    verdict = Worksheet(title="t", question="q", base_sha="x").score(
        novel_true=40, wrong_at_verified=1
    )
    assert verdict.passed is False
    assert "NOT met" in verdict.summary()


# -- what counts as a claim -------------------------------------------------


def test_a_dotted_path_does_not_end_a_sentence():
    """`repo_index.py` would otherwise split into two claims, both nonsense."""
    claims = split_claims("Symbols come from `index/repo_index.py` at build time.")
    assert claims == ["Symbols come from `index/repo_index.py` at build time."]


def test_a_quoted_string_containing_prose_does_not_split():
    """Model prose quotes literals, and a literal can hold a sentence end."""
    text = "The report says `Stopped. All sections written` when the ceiling is hit."
    assert split_claims(text) == [text]


def test_sentences_split_into_separate_claims():
    claims = split_claims("The cap is 60 seconds. Retries stop after five attempts.")
    assert len(claims) == 2


def test_each_bullet_is_its_own_claim():
    """A wrong item hides behind four right ones if a list is marked as a unit."""
    claims = split_claims("It does two things:\n\n- doubles the delay\n- clamps at 60")
    assert "doubles the delay" in claims
    assert "clamps at 60" in claims


def test_a_version_number_does_not_end_a_sentence():
    assert len(split_claims("It targets pydantic 2.13 and nothing older.")) == 1


# -- what the reader is given ----------------------------------------------


def test_a_claim_carries_the_source_its_anchor_points_at(index, repo):
    """The expensive part of checking a claim is navigating, not deciding."""
    sheet = build(_doc(index, _section(index, "The delay is capped at 60 seconds.")), index, repo)
    anchor = sheet.claims[0].anchors[0]
    assert "min(2 ** attempt, 60)" in anchor.source
    assert anchor.location == "retry.py:4-5"
    assert anchor.staleness == "fresh"


def test_the_source_is_cut_at_the_anchors_own_lines(index, repo):
    """Showing the whole file is the same as showing nothing."""
    sheet = build(_doc(index, _section(index, "A claim.")), index, repo)
    assert "MAX_RETRIES" not in sheet.claims[0].anchors[0].source


def test_a_stale_anchor_says_so_next_to_the_claim(index, repo):
    """Judging a claim against code that has since changed produces a wrong
    verdict in either direction."""
    (repo.root / "retry.py").write_text(SOURCE.replace("2 ** attempt, 60", "3 ** attempt, 90"))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-q", "-m", "change")
    before = build_index(Repository(repo.root))

    doc = _doc(before, _section(before, "A claim."))
    (repo.root / "retry.py").write_text(SOURCE)
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-q", "-m", "revert")
    after = build_index(Repository(repo.root))

    sheet = build(doc, after, Repository(repo.root))
    assert sheet.claims[0].anchors[0].staleness == "stale"


def test_source_that_cannot_be_read_says_why_instead_of_vanishing(index):
    sheet = build(_doc(index, _section(index, "A claim.")), index, repo=None)
    view = sheet.claims[0].anchors[0]
    assert view.source == ""
    assert "no repository given" in view.unavailable


# -- scope ------------------------------------------------------------------


def test_background_sections_are_not_judged(index, repo):
    """They describe somebody else's library; counting them inflates novelty
    with facts that are not about this repository."""
    doc = _doc(
        index,
        _section(index, "A repository claim.", id="s1"),
        Section(
            id="s2",
            title="About retries",
            kind="background",
            body="Most libraries add jitter.",
            confidence="derived",
            evidence=(Evidence(kind="external", ref="https://example.com"),),
        ),
    )
    sheet = build(doc, index, repo)
    assert {c.section_id for c in sheet.claims} == {"s1"}


def test_only_verified_claims_are_held_to_the_zero_wrong_bar(index, repo):
    """A wrong `speculative` claim is the document hedging, which is it working."""
    doc = _doc(
        index,
        _section(index, "Confident.", confidence="verified", id="s1"),
        _section(index, "Hedged.", confidence="speculative", id="s2", title="Guess"),
    )
    sheet = build(doc, index, repo)
    assert len(sheet.claims) == 2
    assert [c.section_id for c in sheet.verified_claims] == ["s1"]


def test_the_sheet_carries_the_capability_caveats(index, repo):
    """A reader judging novelty needs to know what the run could not see."""
    doc = Document(
        title="t",
        question="q",
        capabilities=Capabilities(base_sha=index.sha, git="shallow", dirty=True),
        sections=(_section(index, "A claim."),),
    )
    sheet = build(doc, index, repo)
    assert sheet.caveats
    assert any("hallow" in c for c in sheet.caveats)


def test_the_thresholds_are_data_not_prose():
    """So they appear in every sheet rather than drifting to wherever the
    current output happens to land."""
    assert NOVEL_TRUE_REQUIRED == 3


# -- the rendered sheet -----------------------------------------------------


def test_the_sheet_arrives_with_every_box_empty(index, repo):
    """A suggested verdict would anchor the reader to the pipeline's own
    opinion, which is the thing being tested."""
    out = render(build(_doc(index, _section(index, "A claim.")), index, repo))
    assert "☐" in out
    assert "☑" not in out and "☒" not in out
    assert "PASS" not in out and "FAIL" not in out


def test_the_source_is_in_the_page_not_behind_a_link(index, repo):
    """A link is a decision to open a file, and sixty of those is a gate
    nobody finishes."""
    out = render(build(_doc(index, _section(index, "A claim.")), index, repo))
    assert "min(2 ** attempt, 60)" in out
    assert "<pre>" in out


def test_the_sheet_carries_no_script(index, repo):
    out = render(build(_doc(index, _section(index, "A claim.")), index, repo)).lower()
    assert "<script" not in out and "javascript:" not in out
    assert "onclick=" not in out and "onerror=" not in out


def test_model_authored_prose_is_escaped_in_the_sheet(index, repo):
    """Claim text is written by a model that read repository source."""
    doc = _doc(index, _section(index, "Consider <script>alert('x')</script> here."))
    out = render(build(doc, index, repo))
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_the_markdown_sheet_marks_which_claims_carry_certainty(index, repo):
    doc = _doc(
        index,
        _section(index, "Confident.", confidence="verified", id="s1"),
        _section(index, "Hedged.", confidence="speculative", id="s2", title="Guess"),
    )
    out = render_markdown(build(doc, index, repo))
    assert "- [!] Confident." in out
    assert "- [ ] Hedged." in out


# -- the command ------------------------------------------------------------


def test_the_command_exits_zero_even_for_a_useless_document(index, repo, tmp_path):
    """An exit code would be the pipeline grading itself on the axis it is
    being tested for. The sheet is the output; the verdict is the reader's."""
    doc = _doc(index, _section(index, "`backoff_seconds` is a function.", cite=True))
    source = tmp_path / "doc.json"
    doc.write(source)
    out = tmp_path / "sheet.html"
    assert main(["--repo", str(repo.root), "gate-b", str(source), "--out", str(out)]) == 0
    assert "backoff_seconds" in out.read_text()


def test_the_command_reports_counts_without_a_verdict(index, repo, tmp_path, capsys):
    doc = _doc(index, _section(index, "One. Two. Three."))
    source = tmp_path / "doc.json"
    doc.write(source)
    assert main(["--repo", str(repo.root), "--json", "gate-b", str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"] == 3
    assert payload["thresholds"] == {"novel_true": 3, "wrong_at_verified": 0}
    assert "passed" not in payload and "verdict" not in payload
