"""Tests for pinned-SHA repository access.

Two guarantees under test, both of which v0.1 lacked:

  * content is read at a pinned commit, so a run is unaffected by the user
    continuing to edit for the twenty minutes it takes;
  * files that look like credentials never reach a model or an artifact.
"""

from __future__ import annotations

import subprocess

import pytest

from kreb.repo.access import (
    GitError,
    Repository,
    is_secret_path,
    is_vendored,
)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


@pytest.fixture()
def repo(tmp_path):
    """A small repo with two commits, a secret, and a vendored file."""
    root = tmp_path / "sample"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    (root / "app.py").write_text("def one():\n    return 1\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-secret-value\n")
    (root / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    vendor = root / "vendor" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "big.py").write_text("# vendored\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")

    (root / "app.py").write_text("def one():\n    return 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    return root


def test_resolves_and_pins_a_sha(repo):
    r = Repository(repo)
    assert len(r.sha) == 40
    assert r.caps.base_sha == r.sha


def test_reads_content_at_the_pinned_sha_not_the_worktree(repo):
    """The central guarantee: editing during a run cannot change what is read."""
    r = Repository(repo)
    assert b"return 2" in r.read("app.py")

    # The user keeps working while the run is in flight.
    (repo / "app.py").write_text("def one():\n    return 999\n")

    assert b"return 2" in r.read("app.py"), "read fell through to the working tree"
    assert b"999" not in r.read("app.py")


def test_reads_an_older_commit_when_pinned_there(repo):
    r = Repository(repo, rev="HEAD~1")
    assert b"return 1" in r.read("app.py")


def test_dirty_worktree_is_detected_and_recorded(repo):
    (repo / "app.py").write_text("uncommitted\n")
    r = Repository(repo)
    assert r.caps.dirty is True
    assert any("dirty" in w for w in r.caps.warnings())


def test_clean_worktree_is_not_flagged(repo):
    assert Repository(repo).caps.dirty is False


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "config/deploy.pem",
        "keys/id_ed25519",
        "app.key",
        "secrets.yml",
        "config/secrets.yaml",
        ".netrc",
        "backup.kdbx",
    ],
)
def test_secret_paths_are_recognised(path):
    assert is_secret_path(path), f"{path} not recognised as a secret"


@pytest.mark.parametrize(
    "path", ["app.py", "src/keyboard.py", "docs/environment.md", "test_env.py", "monkey.pyc"]
)
def test_ordinary_paths_are_not_flagged_as_secrets(path):
    assert not is_secret_path(path), f"{path} wrongly flagged as a secret"


def test_secret_files_are_excluded_from_enumeration(repo):
    r = Repository(repo)
    files = r.files()
    assert "app.py" in files
    assert ".env" not in files
    assert "deploy.pem" not in files
    assert r.caps.skipped_secrets == 2


def test_reading_a_secret_is_refused_outright(repo):
    """Defence in depth: even a direct read must fail, not just enumeration."""
    r = Repository(repo)
    with pytest.raises(GitError, match="secret"):
        r.read(".env")


# ---------------------------------------------------------------------------
# Vendored and generated code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "vendor/lib/big.py",
        "third_party/x.py",
        "node_modules/pkg/index.js",
        "proto/service_pb2.py",
        "static/bundle.min.js",
    ],
)
def test_vendored_and_generated_are_recognised(path):
    assert is_vendored(path)


@pytest.mark.parametrize("path", ["src/app.py", "vendors.py", "my_vendor_client.py"])
def test_ordinary_paths_are_not_vendored(path):
    assert not is_vendored(path)


def test_vendored_excluded_by_default_and_optional(repo):
    r = Repository(repo)
    assert "vendor/lib/big.py" not in r.files()
    assert "vendor/lib/big.py" in r.files(include_vendored=True)


# ---------------------------------------------------------------------------
# Capability reporting
# ---------------------------------------------------------------------------

def test_shallow_clone_is_detected_and_warned(tmp_path, repo):
    """A shallow clone silently produces no archaeology — that must be visible."""
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone))

    r = Repository(clone)
    assert r.caps.shallow is True
    assert any("Shallow" in w for w in r.caps.warnings())


def test_non_repository_is_rejected(tmp_path):
    with pytest.raises(GitError, match="not a git repository"):
        Repository(tmp_path)


def test_missing_file_reports_absent(repo):
    r = Repository(repo)
    assert r.exists("app.py")
    assert not r.exists("nope.py")


def test_commit_graph_write_succeeds(repo):
    assert Repository(repo).ensure_commit_graph() is True
