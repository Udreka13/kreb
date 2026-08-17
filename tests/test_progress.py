"""Tests for progress reporting.

Two properties here are load-bearing beyond this module.

`test_the_engine_never_writes_to_stdout` guards the stream split. stdout carries
the contract — artifact paths and `--json` payloads — so an adapter piping it
into a parser must never receive progress chatter. The split is the only reason
a human can watch a run whose output is being consumed by a machine.

`test_event_data_may_carry_its_own_kind` guards a trap rather than a feature. An
event *about* a section naturally carries that section's `kind`, which collides
with the event's own `kind` unless the framing parameters are positional-only.
The collision raises at emit time, inside the loop, on a code path only a
rationale section reaches.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest
from test_research import ScriptedProvider, _draft, _metered

from kreb.budget.ledger import Ledger
from kreb.budget.policy import Budget
from kreb.index.repo_index import build_index
from kreb.progress import (
    ConsoleReporter,
    Event,
    JsonlReporter,
    NullReporter,
    Progress,
    Recorder,
    reporter_for,
)
from kreb.repo.access import Repository
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

SOURCE = "MAX_RETRIES = 5\n\n\ndef backoff_seconds(attempt):\n    return min(2 ** attempt, 60)\n"
REF = "retry.py#backoff_seconds"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True, env=_ENV)
    (root / "retry.py").write_text(SOURCE)
    for args in (["add", "-A"], ["commit", "-q", "-m", "first"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=_ENV)
    return Repository(root)


@pytest.fixture()
def index(repo):
    return build_index(repo)


def _plan(n=2, kind="structure"):
    return [
        PlannedSection(id=f"s{i}", title=f"S{i}", kind=kind, refs=[REF]) for i in range(n)
    ]


def _run(index, repo, *, plan=None, provider=None, store=None, budget=None, ledger=None):
    recorder = Recorder()
    report = run_research(
        plan=plan or _plan(),
        question="q",
        index=index,
        repo=repo,
        provider=provider or _metered(
            ScriptedProvider(_draft(cites=[REF])), budget=budget, ledger=ledger
        ),
        store=store,
        reporter=recorder,
    )
    return recorder, report


# -- the stream split -------------------------------------------------------


def test_the_engine_never_writes_to_stdout(index, repo, capsys):
    """stdout is the contract; progress is commentary."""
    stream = io.StringIO()
    run_research(
        plan=_plan(),
        question="q",
        index=index,
        repo=repo,
        provider=_metered(ScriptedProvider(_draft(cites=[REF]))),
        reporter=ConsoleReporter(stream, tty=False),
    )
    assert capsys.readouterr().out == ""
    assert stream.getvalue(), "the reporter was given a stream and wrote nothing to it"


def test_a_run_with_no_reporter_is_silent(index, repo, capsys):
    """Library use must not print. The default is nothing, not stderr."""
    run_research(
        plan=_plan(),
        question="q",
        index=index,
        repo=repo,
        provider=_metered(ScriptedProvider(_draft(cites=[REF]))),
    )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# -- the event stream -------------------------------------------------------


def test_a_run_reports_start_every_section_and_finish(index, repo):
    recorder, report = _run(index, repo)
    kinds = [e.kind for e in recorder.events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert len(recorder.of_kind("section_started")) == 2
    assert len(recorder.of_kind("section_done")) == 2
    assert report.written == ["s0", "s1"]


def test_position_advances_and_the_total_is_the_plan(index, repo):
    recorder, _ = _run(index, repo, plan=_plan(3))
    started = recorder.of_kind("section_started")
    assert [e.done for e in started] == [1, 2, 3]
    assert {e.total for e in recorder.events} == {3}
    assert started[-1].fraction == 1.0


def test_sequence_numbers_are_monotonic(index, repo):
    """A consumer that reorders or drops events needs to be able to tell."""
    recorder, _ = _run(index, repo)
    seqs = [e.seq for e in recorder.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_event_data_may_carry_its_own_kind(index, repo):
    """A rationale section's `kind` must not collide with the event's."""
    progress = Progress(Recorder(), total=1)
    progress.emit("section_started", id="s0", kind="rationale")  # must not raise

    recorder, _ = _run(index, repo, plan=_plan(1, kind="rationale"))
    assert recorder.of_kind("section_started")[0].data["kind"] == "rationale"


# -- what the events have to say -------------------------------------------


def test_every_attempt_is_reported_and_a_rejection_says_why(index, repo):
    """A section that took three tries is a fact about the run, not latency."""
    inner = ScriptedProvider(
        _draft(cites=["retry.py#invented"]),
        _draft(cites=[REF]),
    )
    recorder, _ = _run(index, repo, plan=_plan(1), provider=_metered(inner))

    attempts = recorder.of_kind("attempt")
    assert [a.data["attempt"] for a in attempts] == [1, 2]
    assert attempts[0].data["failed"] is True
    assert "fabricated" in attempts[0].data["reason"]
    assert attempts[1].data["failed"] is False
    assert recorder.of_kind("section_done")[0].data["attempts"] == 2


def test_a_section_reports_its_own_cost_and_status(index, repo):
    recorder, _ = _run(index, repo, plan=_plan(1))
    done = recorder.of_kind("section_done")[0].data
    assert done["status"] == "written"
    assert done["cost"] == pytest.approx(0.1)
    assert done["elapsed"] >= 0.0


def test_a_failed_section_is_reported_as_failed(index, repo):
    inner = ScriptedProvider(_draft(cites=["retry.py#invented"]))
    recorder, report = _run(index, repo, plan=_plan(1), provider=_metered(inner))
    assert recorder.of_kind("section_done")[0].data["status"] == "failed"
    assert report.failed


def test_reuse_is_reported_as_reuse_not_as_a_free_write(index, repo, tmp_path):
    """A near-free second run must read as cache, not as a run that did nothing."""
    store = ArtifactStore(tmp_path / ".kreb")
    _run(index, repo, plan=_plan(1), store=store)

    recorder, _ = _run(index, repo, plan=_plan(1), store=store)
    done = recorder.of_kind("section_done")[0].data
    assert done["status"] == "reused"
    assert done["cost"] == 0.0
    assert recorder.of_kind("attempt") == []


def test_archaeology_is_announced_because_it_stalls_for_free(index, repo):
    """The pickaxe is the slowest step and costs nothing, so silence there
    is indistinguishable from a hang."""
    recorder, _ = _run(index, repo, plan=_plan(1, kind="rationale"))
    assert recorder.of_kind("archaeology")


def test_a_budget_stop_is_announced_and_the_run_still_finishes(index, repo):
    """A truncated run that never says it stopped is the worst outcome."""
    provider = _metered(
        ScriptedProvider(_draft(cites=[REF]), cost=1.0),
        budget=Budget(max_per_run=1.5),
        ledger=Ledger(),
    )
    recorder, report = _run(index, repo, plan=_plan(5), provider=provider)

    stopped = recorder.of_kind("run_stopped")
    assert stopped and "resumed" in stopped[0].message
    assert recorder.events[-1].kind == "run_finished"
    assert recorder.events[-1].data["skipped"] == len(report.skipped) == 3


def test_the_finish_event_totals_the_run(index, repo):
    recorder, report = _run(index, repo)
    finished = recorder.events[-1].data
    assert finished["written"] == 2
    assert finished["cost"] == pytest.approx(report.cost)


# -- sinks ------------------------------------------------------------------


def test_jsonl_emits_one_parseable_object_per_line(index, repo):
    stream = io.StringIO()
    run_research(
        plan=_plan(),
        question="q",
        index=index,
        repo=repo,
        provider=_metered(ScriptedProvider(_draft(cites=[REF]))),
        reporter=JsonlReporter(stream),
    )
    lines = stream.getvalue().strip().split("\n")
    events = [json.loads(line) for line in lines]
    assert events[0]["kind"] == "run_started"
    assert all({"kind", "seq", "done", "total", "data"} <= set(e) for e in events)


def test_the_console_does_not_redraw_into_a_pipe(index, repo):
    """Carriage returns are how a terminal stays tidy and how a log gets litter."""
    stream = io.StringIO()
    run_research(
        plan=_plan(),
        question="q",
        index=index,
        repo=repo,
        provider=_metered(ScriptedProvider(_draft(cites=[REF]))),
        reporter=ConsoleReporter(stream, tty=False),
    )
    out = stream.getvalue()
    assert "\r" not in out
    assert "s0" in out and "written" in out and "done ·" in out


def test_the_running_total_agrees_with_the_final_total_across_a_retry(index, repo):
    """A retried section is billed twice; counting both the attempts and the
    section's ledger delta reports the retry cost twice over."""
    stream = io.StringIO()
    report = run_research(
        plan=_plan(2),
        question="q",
        index=index,
        repo=repo,
        provider=_metered(
            ScriptedProvider(_draft(cites=["retry.py#invented"]), _draft(cites=[REF]))
        ),
        reporter=ConsoleReporter(stream, tty=False),
    )
    running = [line for line in stream.getvalue().split("\n") if "total)" in line]
    assert f"${report.cost:.5f} total" in running[-1]


def test_a_long_section_id_keeps_its_distinguishing_tail():
    """`research.loop.run…` looks like every other section in that module."""
    stream = io.StringIO()
    console = ConsoleReporter(stream, tty=False)
    console.emit(
        Event(
            kind="section_done",
            done=1,
            total=1,
            data={"id": "research.loop.run_research_and_report", "status": "written"},
        )
    )
    assert "run_research_and_report" in stream.getvalue()


def test_the_console_redraws_in_place_on_a_terminal():
    stream = io.StringIO()
    console = ConsoleReporter(stream, tty=True)
    console.emit(Event(kind="section_started", done=1, total=2, data={"id": "s0"}))
    console.emit(
        Event(kind="section_done", done=1, total=2, data={"id": "s0", "status": "written"})
    )
    out = stream.getvalue()
    assert "\r" in out
    assert out.count("s0") == 2


def test_auto_is_quiet_when_stderr_is_not_a_terminal():
    """An unattended run should not fill a log with lines nobody reads."""
    assert isinstance(reporter_for("auto", io.StringIO()), NullReporter)


def test_plain_reports_even_into_a_pipe():
    assert isinstance(reporter_for("plain", io.StringIO()), ConsoleReporter)


def test_none_reports_nothing():
    assert isinstance(reporter_for("none", io.StringIO()), NullReporter)


def test_an_unknown_sink_falls_back_to_the_console_rather_than_to_silence():
    """Losing progress to a typo is worse than printing more than asked."""
    assert isinstance(reporter_for("chatty", io.StringIO()), ConsoleReporter)
