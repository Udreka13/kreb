"""Pinned-SHA repository access.

Two holes in v0.1 that this closes.

**The dirty tree.** A research run takes twenty minutes; the user keeps coding.
Reading through `open()` means a section validated against one version of a
symbol and anchored against another, and staleness computed against a dirty tree
is meaningless. So the SHA is resolved once at run start and every byte is read
through `git cat-file` at that SHA.

**Secret egress.** The agent reads the repository and quotes it into an artifact
the user then commits or publishes. `.env` files, fixtures with live tokens, a
hardcoded key in a 2019 commit. Enumeration goes through `git ls-files`, so
`.gitignore` is respected by construction, plus a denylist for the files people
routinely commit by accident.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Environment pinned so output is parseable and reproducible regardless of the
# user's git config, locale, timezone, pager or colour settings.
_GIT_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}

_GIT_FLAGS = ["--no-pager", "--no-optional-locks", "-c", "color.ui=false"]

# Files that should never reach a model or an artifact, regardless of whether
# git tracks them. Matched against the basename and the full path.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.keystore",
    "*.jks",
    "credentials",
    "credentials.*",
    ".netrc",
    ".pgpass",
    "*.kdbx",
    "secrets.y*ml",
    "*secrets.y*ml",
)

# Directories whose contents are committed but not authored here. They would
# otherwise dominate centrality metrics and produce `verified` anchors into
# generated files.
VENDOR_DIRS = (
    "vendor",
    "third_party",
    "thirdparty",
    "node_modules",
    ".venv",
    "venv",
)

_GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", ".min.js", ".min.css", ".g.dart")


class GitError(RuntimeError):
    """A git invocation failed. Carries stderr, because git's messages are good."""


@dataclass
class Capabilities:
    """What this run can actually see.

    Surfaced in every rendered artifact. When `gh` is unauthenticated or the
    clone is shallow, the document looks exactly as authoritative with far less
    behind it — which is the failure kreb exists to prevent, arriving silently.
    """

    git: bool = True
    shallow: bool = False
    dirty: bool = False
    base_sha: str = ""
    languages: list[str] = field(default_factory=list)
    tracked_files: int = 0
    skipped_secrets: int = 0
    skipped_vendored: int = 0
    degraded_files: int = 0

    def warnings(self) -> list[str]:
        out = []
        if self.shallow:
            out.append(
                "Shallow clone: history is truncated, so rationale archaeology "
                "will find little and the document may paraphrase the code."
            )
        if self.dirty:
            out.append(
                f"Working tree is dirty; analysed the committed state at {self.base_sha[:12]}."
            )
        if self.skipped_secrets:
            out.append(f"Skipped {self.skipped_secrets} file(s) matching secret patterns.")
        return out


def _run(args: list[str], cwd: Path, *, binary: bool = False):
    proc = subprocess.run(
        ["git", *_GIT_FLAGS, *args],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:3])} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def is_secret_path(path: str) -> bool:
    """True if `path` looks like it holds credentials."""
    name = Path(path).name
    return any(
        fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat) for pat in SECRET_PATTERNS
    )


def is_vendored(path: str) -> bool:
    parts = Path(path).parts
    if any(part in VENDOR_DIRS for part in parts):
        return True
    return path.endswith(_GENERATED_SUFFIXES)


class Repository:
    """Read-only access to one repository at one pinned commit."""

    def __init__(self, root: Path | str, *, rev: str = "HEAD") -> None:
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            raise GitError(f"not a git repository: {self.root}")
        self.sha = _run(["rev-parse", rev], self.root).strip()
        self.caps = Capabilities(base_sha=self.sha)
        self.caps.shallow = (self.root / ".git" / "shallow").exists()
        self.caps.dirty = bool(_run(["status", "--porcelain"], self.root).strip())

    # -- enumeration -------------------------------------------------------

    def files(self, *, include_vendored: bool = False) -> list[str]:
        """Tracked paths at the pinned commit, secrets and vendored code removed.

        Uses `ls-tree` at the SHA rather than `ls-files`, so the file list
        matches the content that will actually be read — an untracked-but-added
        file cannot sneak in, and a file deleted since the SHA cannot vanish
        mid-run.
        """
        raw = _run(["ls-tree", "-r", "--name-only", "-z", self.sha], self.root)
        paths = [p for p in raw.split("\0") if p]

        kept: list[str] = []
        for path in paths:
            if is_secret_path(path):
                self.caps.skipped_secrets += 1
                continue
            if not include_vendored and is_vendored(path):
                self.caps.skipped_vendored += 1
                continue
            kept.append(path)

        self.caps.tracked_files = len(kept)
        return kept

    # -- content -----------------------------------------------------------

    def read(self, path: str) -> bytes:
        """Read one file's bytes at the pinned commit. Never touches the worktree."""
        if is_secret_path(path):
            raise GitError(f"refusing to read a path matching a secret pattern: {path}")
        return _run(["cat-file", "-p", f"{self.sha}:{path}"], self.root, binary=True)

    def exists(self, path: str) -> bool:
        try:
            _run(["cat-file", "-e", f"{self.sha}:{path}"], self.root)
            return True
        except GitError:
            return False

    # -- history performance ----------------------------------------------

    def ensure_commit_graph(self) -> bool:
        """Write a commit-graph with changed-path Bloom filters.

        The binding constraint on archaeology is local git wall-clock, not forge
        rate limits: `git log -S` walks the whole history diffing every commit.
        Bloom filters make pathspec-limited walks roughly an order of magnitude
        faster, and this is a one-off cost at index time.
        """
        try:
            _run(["commit-graph", "write", "--reachable", "--changed-paths"], self.root)
            return True
        except GitError:
            # Not fatal — archaeology is slower, nothing is wrong.
            return False
