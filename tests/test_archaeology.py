"""Tests for git and forge archaeology.

The central test is `test_introduction_is_not_last_touch`. Its fixture is built
so that **blame and the pickaxe disagree**: a later refactor owns the majority
of the symbol's lines, while the decision being documented was made three
commits earlier. A blame-based implementation passes every smoke test and gets
this one wrong, confidently and with a real commit SHA attached — which is the
failure this module exists to prevent, so it is the failure that gets a test.
"""

from __future__ import annotations

import subprocess

import pytest

from kreb.archaeology.forge import (
    ForgeEvidence,
    ForgeStatus,
    GitHubForge,
    enrich,
    parse_remote,
)
from kreb.archaeology.history import (
    Commit,
    find_introducing_commit,
    find_reverts,
    pickaxe_line,
    symbol_history,
)
from kreb.index.symbols import extract_symbols
from kreb.repo.access import Repository

_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

# The line the pickaxe must find. Long and unique, so it is the unambiguous
# choice of `pickaxe_line`, and it has survived untouched since commit 1.
DECISION_LINE = 'raise DeadlineExceeded("backoff ceiling reached before the request completed")'

V1 = f"""BACKOFF_CEILING_SECONDS = 47.5


def next_delay(attempt, base):
    scaled = base * attempt
    if scaled > BACKOFF_CEILING_SECONDS:
        {DECISION_LINE}
    return scaled
"""

# Four of the symbol's seven lines now belong to this commit, against three for
# the commit that made the decision. Blame's majority answer is therefore wrong.
V2 = f"""BACKOFF_CEILING_SECONDS = 47.5


def next_delay(attempt, base_delay):
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    scaled = base_delay * attempt
    if scaled > BACKOFF_CEILING_SECONDS:
        {DECISION_LINE}
    return scaled
"""

V3 = V2.replace("47.5", "60.0")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_ENV)


def _commit(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture()
def history_repo(tmp_path):
    """A repo whose blame majority and true introduction are different commits."""
    root = tmp_path / "svc"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    (root / "README.md").write_text("# svc\n")
    (root / "retry.py").write_text(V1)
    _commit(root, "Add retry policy with a hard backoff ceiling")

    (root / "retry.py").write_text(V2)
    _commit(root, "Guard against negative attempts\n\nFixes #42. See also #7.")

    (root / "retry.py").write_text(V3)
    _commit(root, "Bump the ceiling to 60s")

    (root / "retry.py").write_text(V2)
    _commit(root, 'Revert "Bump the ceiling to 60s"')

    return Repository(root)


def _next_delay_range(repo):
    source = repo.read("retry.py")
    symbol = next(s for s in extract_symbols(source, "python") if s.name == "next_delay")
    return source, symbol.start_line, symbol.end_line


# -- the governing rule ----------------------------------------------------


def test_introduction_is_not_last_touch(history_repo):
    """The decision commit wins over the commit owning most of the lines."""
    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)

    assert history.introduced is not None
    assert history.introduced.commit.subject.startswith("Add retry policy")
    # Precisely the wrong answer a blame-only implementation would give:
    assert "Guard against negative attempts" not in history.introduced.commit.subject


def test_blame_majority_really_is_the_wrong_commit(history_repo):
    """Guards the fixture itself.

    If a git version change made blame attribute these lines differently, the
    test above would start passing for no reason. This asserts the trap is
    still armed.
    """
    from kreb.archaeology.history import blame_lines

    source, start, end = _next_delay_range(history_repo)
    blamed = blame_lines(history_repo, "retry.py", start, end)
    top_sha = max(blamed.items(), key=lambda kv: kv[1])[0]

    from kreb.archaeology.history import commit_info

    top = commit_info(history_repo, top_sha)
    assert top is not None
    assert top.subject.startswith("Guard against negative attempts")


def test_introduction_is_verified_when_corroborated(history_repo):
    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)

    assert history.introduced.confidence == "verified"
    assert history.introduced.method == "pickaxe+blame"


def test_refactor_appears_as_modification_not_introduction(history_repo):
    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)

    subjects = [m.commit.subject for m in history.modifications]
    assert any(s.startswith("Guard against negative attempts") for s in subjects)
    assert history.introduced.commit.sha not in {m.commit.sha for m in history.modifications}


# -- saturation ------------------------------------------------------------


def test_saturated_pickaxe_is_reported_not_hidden(history_repo):
    """A truncated search must not present its oldest commit as the origin."""
    saturated = find_introducing_commit(
        history_repo, "retry.py", "BACKOFF_CEILING_SECONDS = 47.5", max_count=1
    )
    assert saturated.saturated is True

    full = find_introducing_commit(
        history_repo, "retry.py", "BACKOFF_CEILING_SECONDS = 47.5", max_count=40
    )
    assert full.saturated is False
    assert full.commit.subject.startswith("Add retry policy")
    # The bounded search stopped somewhere later than the real introduction —
    # exactly the case that must never be labelled `verified`.
    assert saturated.commit.sha != full.commit.sha


def test_saturation_downgrades_confidence(history_repo, monkeypatch):
    """Corroboration must not rescue a truncated search.

    The commit here is real and blame does agree it touched these lines — so the
    naive `corroborated -> verified` rule fires. It must not: blame agreeing
    that a commit touched the range says nothing about whether an earlier,
    unsearched commit introduced it, so the two signals are not independent.
    """
    import kreb.archaeology.history as history_module

    real = history_module.find_introducing_commit

    def saturating(repo, path, needle, **kwargs):
        found = real(repo, path, needle, **kwargs)
        return history_module.Pickaxe(commit=found.commit, saturated=True)

    monkeypatch.setattr(history_module, "find_introducing_commit", saturating)

    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)

    assert history.introduced.confidence != "verified"
    assert history.truncated is True
    assert "limit" in history.note


# -- pickaxe line selection ------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "",
        "    ",
        "}",
        "    })",
        "    pass",
        "    return",
        "    # a comment that is quite long indeed",
        "    // another long trailing comment here",
        "from kreb.repo.access import Repository",
        "import collections.abc",
    ],
)
def test_boilerplate_is_never_pickaxed(line):
    assert pickaxe_line([line]) is None


@pytest.mark.parametrize(
    "line",
    [
        "    return self._resolve(policy, fallback_delay)",
        "export function nextDelay(attempt: number): number {",
        "    scaled = base_delay * attempt",
    ],
)
def test_distinctive_lines_survive(line):
    """`return` and `export` prefix real content; only bare forms are rejected."""
    assert pickaxe_line([line]) == line.strip()


def test_longest_distinctive_line_wins():
    lines = ["    x = 1", "    " + DECISION_LINE, "    return scaled"]
    assert pickaxe_line(lines) == DECISION_LINE


# -- reverts ---------------------------------------------------------------


def test_reverts_are_found(history_repo):
    reverts = find_reverts(history_repo, "retry.py")
    assert [c.subject for c in reverts] == ['Revert "Bump the ceiling to 60s"']


# -- degradation -----------------------------------------------------------


def test_shallow_clone_degrades_with_a_note(history_repo, monkeypatch):
    monkeypatch.setattr(history_repo.caps, "shallow", True)
    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)

    assert history.introduced is None
    assert history.truncated is True
    assert "shallow" in history.note


def test_missing_path_degrades_rather_than_raises(history_repo):
    history = symbol_history(
        history_repo, "gone.py#thing", "gone.py", 1, 5, b"def thing():\n    pass\n"
    )
    assert history.truncated is True
    assert history.note
    assert history.introduced is None


# -- issue references ------------------------------------------------------


def test_issue_numbers_are_mined_from_messages(history_repo):
    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)
    assert 42 in history.issue_numbers
    assert 7 in history.issue_numbers


def test_referenced_issues_dedupes_and_sorts():
    commit = Commit(
        sha="a" * 40,
        author="t",
        date="2024-01-01",
        subject="Fix thing (#12)",
        body="Closes #12\nrefs #3\nnot-an-issue #1234567",
    )
    assert commit.referenced_issues() == [3, 12]


# -- forge -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:Udreka13/kreb.git", ("Udreka13", "kreb")),
        ("https://github.com/Udreka13/kreb.git", ("Udreka13", "kreb")),
        ("https://github.com/Udreka13/kreb", ("Udreka13", "kreb")),
        ("ssh://git@github.com/org/some-repo.git", ("org", "some-repo")),
    ],
)
def test_remote_parsing(url, expected):
    assert parse_remote(url) == expected


@pytest.mark.parametrize("url", ["git@gitlab.com:x/y.git", "/local/path", ""])
def test_non_github_remotes_are_declined(url):
    assert parse_remote(url) is None


def test_no_forge_still_produces_evidence_and_says_so():
    evidence = enrich(object(), None)
    assert isinstance(evidence, ForgeEvidence)
    assert evidence.pull_requests == []
    assert evidence.status.available is False
    assert "unavailable" in evidence.status.describe()


def test_rate_limited_status_is_visible_in_the_manifest():
    status = ForgeStatus(available=True, rate_limited=True)
    assert "incomplete" in status.describe()


def test_rate_limit_short_circuits_further_requests(monkeypatch):
    forge = GitHubForge("o", "r", token=None)
    calls = []

    def fake_get(path):
        calls.append(path)
        forge.status.rate_limited = True
        return None

    monkeypatch.setattr(forge, "_get", fake_get)

    assert forge.pull_request_for_commit("a" * 40) is None
    assert forge.pull_request_for_commit("b" * 40) is None
    # The second lookup must not have hit the network at all.
    assert len(calls) == 1


def test_commit_lookups_are_cached(monkeypatch):
    forge = GitHubForge("o", "r", token=None)
    calls = []

    def fake_get(path):
        calls.append(path)
        return [{"number": 5, "title": "t", "body": "b", "html_url": "u", "merged_at": None}]

    monkeypatch.setattr(forge, "_get", fake_get)

    first = forge.pull_request_for_commit("a" * 40)
    second = forge.pull_request_for_commit("a" * 40)
    assert first is second
    assert len(calls) == 1


def test_enrich_does_not_duplicate_a_pr_shared_by_commits(monkeypatch, history_repo):
    forge = GitHubForge("o", "r", token=None)
    monkeypatch.setattr(
        forge,
        "_get",
        lambda path: (
            [{"number": 9, "title": "t", "body": "b", "html_url": "u", "merged_at": None}]
            if "/pulls" in path
            else {"number": 42, "title": "i", "body": "", "html_url": "u"}
        ),
    )

    source, start, end = _next_delay_range(history_repo)
    history = symbol_history(history_repo, "retry.py#next_delay", "retry.py", start, end, source)
    evidence = enrich(history, forge)

    assert [p.number for p in evidence.pull_requests] == [9]
    assert 42 in [i.number for i in evidence.issues]
