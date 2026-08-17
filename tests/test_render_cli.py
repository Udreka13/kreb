"""Tests for the renderers and the command line.

Renderers are dumb transformations, so most of what matters is what they must
not *lose*: the capability manifest, the confidence tier, and the staleness of
each anchor. A renderer that drops those turns a hedged document into a
confident one, which is the same failure as generating a confident document —
arriving one layer later.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kreb.cli import main
from kreb.doc.schema import (
    Anchor,
    Capabilities,
    DiagramSpec,
    Document,
    Evidence,
    Section,
)
from kreb.doc.validate import validate
from kreb.index.repo_index import build_index
from kreb.render import html as html_render
from kreb.render import markdown as md_render
from kreb.repo.access import Repository
from kreb.viz.d2 import asserted_diagram, d2_available, import_diagram, render_svg

_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

SOURCE = "MAX_RETRIES = 5\n\n\ndef backoff_seconds(attempt):\n    return min(2 ** attempt, 60)\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_ENV)


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "retry.py").write_text(SOURCE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


@pytest.fixture()
def index(project):
    return build_index(Repository(project))


@pytest.fixture()
def doc(index):
    symbol = index.symbols["retry.py#backoff_seconds"]
    return Document(
        title="Retry behaviour",
        question="How does backoff work?",
        capabilities=Capabilities(
            base_sha=index.sha,
            git="shallow",
            forge="none",
            languages=("python",),
            degraded_files=2,
            total_files=5,
            dirty=True,
        ),
        sections=(
            Section(
                id="s1",
                title="Backoff",
                kind="structure",
                body="`backoff_seconds` caps the delay at 60 seconds.\n\n"
                "- doubles each attempt\n- clamps at the ceiling",
                confidence="verified",
                anchors=(Anchor(ref=symbol.ref, text_hash=symbol.text_hash, lines=(4, 5)),),
                evidence=(Evidence(kind="symbol", ref=symbol.ref),),
            ),
            Section(
                id="s2",
                title="About exponential backoff",
                kind="background",
                body="Most libraries jitter the delay to avoid thundering herds.",
                confidence="derived",
                evidence=(
                    Evidence(kind="external", ref="https://example.com/backoff", note="overview"),
                ),
            ),
        ),
    )


# -- markdown --------------------------------------------------------------


def test_markdown_leads_with_the_manifest(doc, index):
    out = md_render.render(doc, validate(doc, index))
    manifest_at = out.index("What this document is built on")
    first_section_at = out.index("## Backoff")
    assert manifest_at < first_section_at, "caveats printed after the content have already failed"


def test_markdown_carries_every_capability_warning(doc, index):
    out = md_render.render(doc, validate(doc, index))
    for warning in doc.capabilities.warnings():
        assert warning in out


def test_markdown_marks_confidence_and_labels_background(doc, index):
    out = md_render.render(doc, validate(doc, index))
    assert "verified" in out
    assert "About the library, not this repository" in out


def test_markdown_flags_a_stale_anchor(doc, index, project):
    (project / "retry.py").write_text(SOURCE.replace("2 ** attempt, 60", "3 ** attempt, 90"))
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "change")
    after = build_index(Repository(project))

    out = md_render.render(doc, validate(doc, after))
    assert "changed since this was written" in out


# -- html ------------------------------------------------------------------


def test_html_contains_no_script(doc, index):
    """A product decision: output people commit and open from file:// paths."""
    out = html_render.render(doc, validate(doc, index))
    lowered = out.lower()
    assert "<script" not in lowered
    assert "javascript:" not in lowered
    assert "onerror=" not in lowered and "onclick=" not in lowered


def test_html_is_self_contained(doc, index):
    out = html_render.render(doc, validate(doc, index))
    assert "<style>" in out
    assert "http://" not in out.replace("http://www.w3.org", "")
    assert "cdn" not in out.lower()


def test_html_escapes_model_authored_prose(index):
    """Section bodies come from a model, which read repository source."""
    symbol = index.symbols["retry.py#backoff_seconds"]
    doc = Document(
        title="t",
        capabilities=Capabilities(base_sha=index.sha),
        sections=(
            Section(
                id="s1",
                title="<img src=x onerror=alert(1)>",
                kind="structure",
                body="Consider <script>alert('xss')</script> and `a<b`.",
                anchors=(Anchor(ref=symbol.ref, text_hash=symbol.text_hash),),
            ),
        ),
    )
    out = html_render.render(doc)
    # The property is that no attacker-supplied *markup* survives — the literal
    # text may well appear, escaped and inert, which is exactly correct.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<img" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "<code>a&lt;b</code>" in out


def test_html_boxes_the_background_section(doc, index):
    out = html_render.render(doc, validate(doc, index))
    assert 'class="background"' in out
    assert "About the library, not this repository" in out


def test_html_shows_confidence_as_a_chip(doc, index):
    out = html_render.render(doc, validate(doc, index))
    assert 'class="chip verified"' in out


def test_html_surfaces_the_limits(doc, index):
    out = html_render.render(doc, validate(doc, index))
    assert "Read with these limits in mind" in out
    assert "Shallow clone" in out


def test_html_renders_lists_and_code(doc, index):
    out = html_render.render(doc, validate(doc, index))
    assert "<ul>" in out and "<li>" in out
    assert "<code>backoff_seconds</code>" in out


def test_both_renderers_survive_an_empty_document(index):
    empty = Document(title="Nothing", capabilities=Capabilities(base_sha=index.sha))
    assert "Nothing" in md_render.render(empty)
    assert "Nothing" in html_render.render(empty)


# -- diagrams --------------------------------------------------------------


def test_diagram_provenance_is_printed_next_to_the_diagram(index):
    doc = Document(
        title="t",
        capabilities=Capabilities(base_sha=index.sha),
        sections=(
            Section(
                id="s1", title="Shape", kind="structure", body="text",
                diagram=asserted_diagram("Flow", "a -> b"),
            ),
        ),
    )
    for out in (md_render.render(doc), html_render.render(doc)):
        assert "not read from the code" in out


def test_an_empty_import_graph_produces_no_diagram(index):
    """An empty diagram asserts these modules do not depend on each other."""
    assert import_diagram(index, ["retry.py"]) is None


def test_missing_d2_degrades_with_a_reason():
    spec = asserted_diagram("Flow", "a -> b")
    result = render_svg(spec)
    if d2_available():
        assert result.ok
    else:
        assert not result.ok
        assert "not installed" in result.reason


# -- cli -------------------------------------------------------------------


def test_cli_index_reports_the_pinned_commit(project, capsys):
    assert main(["--repo", str(project), "index"]) == 0
    out = capsys.readouterr().out
    assert "symbols" in out and "commit" in out


def test_cli_index_json_is_machine_readable(project, capsys):
    assert main(["--repo", str(project), "--json", "index"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["symbols"] >= 2
    assert payload["languages"] == ["python"]


def test_cli_map_runs_without_a_key(project, capsys, monkeypatch):
    """The deterministic half must be usable before anyone has an account."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert main(["--repo", str(project), "map"]) == 0
    assert "Repository map" in capsys.readouterr().out


def test_cli_render_writes_a_file(project, doc, tmp_path, capsys):
    source = tmp_path / "doc.json"
    doc.write(source)
    destination = tmp_path / "out" / "doc.html"
    assert main(["render", str(source), "--format", "html", "--out", str(destination)]) == 0
    assert destination.exists()
    assert "<style>" in destination.read_text()
    assert str(destination.resolve()) in capsys.readouterr().out


def test_cli_validate_passes_a_clean_document(project, doc, tmp_path):
    source = tmp_path / "doc.json"
    doc.write(source)
    assert main(["--repo", str(project), "validate", str(source)]) == 0


def test_cli_validate_exits_nonzero_on_a_fabricated_anchor(project, index, tmp_path, capsys):
    bad = Document(
        title="t",
        capabilities=Capabilities(base_sha=index.sha),
        sections=(
            Section(
                id="s1", title="a", kind="structure", body="text",
                anchors=(Anchor(ref="retry.py#invented", text_hash="x"),),
            ),
        ),
    )
    source = tmp_path / "bad.json"
    bad.write(source)
    assert main(["--repo", str(project), "validate", str(source)]) == 1
    assert "fabricated" in capsys.readouterr().out


def test_cli_doc_without_a_key_explains_where_to_put_one(project, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KREB_API_KEY", raising=False)
    monkeypatch.setattr("kreb.config.secrets.from_keyring", lambda: None)
    assert main(["--repo", str(project), "doc", "how does it work?"]) == 2
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err and "kreb.toml" in err


def test_the_plan_follows_the_question_not_just_centrality(project):
    """Ranking by centrality alone answers "what is this built around", which is
    a fine question and usually not the one that was asked — producing a
    plausible-looking non-answer."""
    from kreb.cli import plan_sections
    from kreb.index.repo_map import build_map

    (project / "backoff.py").write_text(
        "def compute_backoff(attempt):\n    return attempt * 2\n"
    )
    (project / "unrelated.py").write_text("def render_template(name):\n    return name\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "more")
    index = build_index(Repository(project))

    plan = plan_sections(build_map(index), index, question="how is backoff computed?", depth=1)
    assert plan
    assert "compute_backoff" in plan[0].refs[0]


@pytest.fixture()
def layered(project):
    """A repository shaped like a real one, at the size where the bugs appear.

    A layered codebase has a domain-model module that everything imports and
    feature code that nothing imports, so centrality ranks the models top and
    the answer bottom. Two details are load-bearing and not decoration:

    *The model module holds more than 80 symbols.* The planner used to score
    only the 80 most central, so on a small repository the answer was inside the
    pool anyway and every ranking bug was invisible. This is the size at which
    the pool starts excluding things.

    *The question matches the target on one term out of five.* Relevance is a
    fraction of the question's terms, so it is small in practice — the real
    measurement was 0.17 against a centrality of 1.00. A fixture where the
    target matches half the question hides the scale mismatch.
    """
    classes = "\n\n".join(
        f"class Entity{n}:\n    name = ''\n    size = 0\n    label = ''" for n in range(30)
    )
    (project / "models.py").write_text(classes + "\n")
    for i in range(6):
        (project / f"consumer{i}.py").write_text(
            f"from models import Entity0\n\n\ndef use{i}(e):\n    return e.name\n"
        )
    (project / "rerank.py").write_text(
        "def rerank_passages(passages):\n    return sorted(passages)\n"
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_rerank.py").write_text(
        "def test_the_reranker_solves_the_ordering_problem():\n    assert True\n"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "layered")
    return project


def _plan_for(project, question, depth=2):
    from kreb.cli import plan_sections
    from kreb.index.repo_map import build_map

    index = build_index(Repository(project))
    return plan_sections(build_map(index), index, question=question, depth=depth)


def test_a_lexical_match_outranks_a_symbol_that_is_merely_central(layered):
    """Relevance is a fraction of the *question's* terms, so it tops out low —
    measured at 0.33 on a real repository — while normalised centrality reaches
    1.00. Unless both are scaled to their own maxima the weights are decorative
    and a perfect match loses to the most-imported symbol."""
    plan = _plan_for(layered, "why does the reranker exist and what problem did it solve?")
    assert plan[0].refs[0] == "rerank.py#rerank_passages", (
        f"the question was answered by {plan[0].refs[0]}"
    )


def test_no_section_is_planned_for_a_class_member(layered):
    """Nearly half the symbols in a repository are members. Five sections on
    `Hub.name`, `.size`, `.label` say together what one on `Hub` says once."""
    plan = _plan_for(layered, "what do the entity name label and size fields mean?")
    dotted = [p.refs[0] for p in plan if "." in p.refs[0].partition("#")[2]]
    assert not dotted, f"planned a section per member: {dotted}"
    assert any(p.refs[0].startswith("models.py#Entity") for p in plan), (
        "the member's owner should stand in for it"
    )


def test_test_files_are_not_planned_as_sections(layered):
    """A test name restates a feature's vocabulary more densely than the feature
    does, so amplifying lexical signal makes tests win."""
    plan = _plan_for(layered, "why does the reranker exist and what problem did it solve?")
    assert not [p for p in plan if "test" in p.refs[0].partition("#")[0]]
    assert plan[0].refs[0] == "rerank.py#rerank_passages"


def test_a_question_with_no_lexical_hits_still_plans_something(project):
    """Degrade to the central symbols rather than to nothing."""
    from kreb.cli import plan_sections
    from kreb.index.repo_map import build_map

    index = build_index(Repository(project))
    plan = plan_sections(build_map(index), index, question="zzzz qqqq", depth=1)
    assert plan


def test_planning_is_reproducible(project):
    """The section writer is the only place nondeterminism may enter."""
    from kreb.cli import plan_sections
    from kreb.index.repo_map import build_map

    index = build_index(Repository(project))
    repo_map = build_map(index)
    first = [p.refs[0] for p in plan_sections(repo_map, index, question="retries", depth=2)]
    second = [p.refs[0] for p in plan_sections(repo_map, index, question="retries", depth=2)]
    assert first == second


def test_cli_reports_errors_without_a_traceback(capsys):
    assert main(["--repo", "/nonexistent", "index"]) == 1
    assert "error:" in capsys.readouterr().err
