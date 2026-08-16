"""Git history mining — the source of *why*.

The one rule that matters here: **never treat last-touch as introduction.**
`git blame` tells you who last modified a line, which is usually a reformat, a
rename, or a lint fix — not the decision that put the behaviour there. Reporting
that commit as the rationale produces a confident, well-cited, wrong answer,
which is the exact failure mode this project exists to avoid.

So the chain is: blame-through (`-w -M -C`, ignoring whitespace and following
moves) to find candidates, then a **pickaxe gate** (`git log -S`) to find the
commit that actually introduced the distinguishing content, then corroboration.

Performance is the binding constraint, not forge rate limits. `git log -S` walks
the whole history diffing every commit, so every call here is pathspec-limited,
count-bounded and wall-clock-bounded, and the commit-graph with Bloom filters is
written once at index time. On a large repository an unbounded pickaxe is
minutes *per symbol*, and it costs $0, so it never shows up in cost accounting —
it just makes the run never finish.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from kreb.repo.access import _GIT_ENV, _GIT_FLAGS, GitError, Repository

# Lines too generic to pickaxe on — they appear everywhere and would match
# thousands of commits.
#
# The alternatives must each consume the *whole* line: a partial match would
# reject `return self._resolve(policy, fallback)` as boilerplate on the strength
# of its first word, and that line is one of the most distinctive in a function.
# `export` is deliberately absent for the same reason — in TypeScript it prefixes
# most real definitions.
_UNDISTINCTIVE = re.compile(
    r"^\s*(?:"
    r"[(){}\[\];,]*"  # punctuation only, including the empty line
    r"|(?:pass|return|break|continue|else|try|end|None|null)\s*:?\s*"
    r"|(?:#|//|\*|/\*).*"  # comment lines
    r"|(?:import|from|package)\s.*"  # import lines: shared across many files
    r")\s*$"
)

DEFAULT_TIMEOUT = 20.0


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    body: str = ""

    @property
    def short(self) -> str:
        return self.sha[:12]

    def referenced_issues(self) -> list[int]:
        """Issue/PR numbers mentioned in the message.

        Works with no forge access at all, which matters: this is the fallback
        that keeps archaeology partially useful offline or unauthenticated.
        """
        text = f"{self.subject}\n{self.body}"
        found = {int(n) for n in re.findall(r"#(\d{1,6})\b", text)}
        for match in re.finditer(
            r"(?:closes|fixes|resolves|refs?)\s+#(\d{1,6})", text, re.IGNORECASE
        ):
            found.add(int(match.group(1)))
        return sorted(found)


@dataclass
class Evidence:
    """One archaeological finding about a symbol."""

    kind: str  # "introduced" | "modified" | "reverted"
    commit: Commit
    confidence: str  # "verified" | "derived" | "speculative"
    method: str  # how it was found — provenance for the claim itself
    lines: tuple[int, int] | None = None


@dataclass
class SymbolHistory:
    ref: str
    introduced: Evidence | None = None
    modifications: list[Evidence] = field(default_factory=list)
    issue_numbers: list[int] = field(default_factory=list)
    truncated: bool = False
    note: str = ""

    @property
    def commits(self) -> list[Commit]:
        out = [self.introduced.commit] if self.introduced else []
        out.extend(m.commit for m in self.modifications)
        return out


def _git(repo: Repository, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            ["git", *_GIT_FLAGS, *args],
            cwd=repo.root,
            env=_GIT_ENV,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git {args[0]} exceeded {timeout}s") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
        )
    return proc.stdout.decode("utf-8", "replace")


def _parse_commit(raw: str) -> Commit | None:
    """Parse one record from `--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b`."""
    parts = raw.split("\x1f")
    if len(parts) < 4 or not parts[0].strip():
        return None
    return Commit(
        sha=parts[0].strip(),
        author=parts[1],
        date=parts[2],
        subject=parts[3],
        body=parts[4] if len(parts) > 4 else "",
    )


_FORMAT = "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"


def commit_info(repo: Repository, sha: str) -> Commit | None:
    raw = _git(repo, ["show", "-s", _FORMAT, sha])
    return _parse_commit(raw.split("\x1e")[0])


def blame_lines(
    repo: Repository, path: str, start: int, end: int, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, int]:
    """Commits that last touched lines `start..end`, with a touch count.

    `-w -M -C` ignores whitespace and follows code moved within and between
    files, which strips out most reformat-only noise. It does *not* make this
    the introducing commit — that is what the pickaxe is for.
    """
    raw = _git(
        repo,
        [
            "blame",
            "-w",
            "-M",
            "-C",
            "--line-porcelain",
            f"-L{start},{end}",
            repo.sha,
            "--",
            path,
        ],
        timeout=timeout,
    )
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        match = re.match(r"^([0-9a-f]{40})\s+\d+\s+\d+", line)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def pickaxe_line(source_lines: list[str]) -> str | None:
    """Choose a distinctive line to search history for.

    Prefers the longest line that is not boilerplate. A generic line
    (`return`, `}`, an import) would match thousands of commits and turn a
    bounded lookup into a full-history scan with a useless answer.
    """
    candidates = [
        line.strip()
        for line in source_lines
        if len(line.strip()) >= 12 and not _UNDISTINCTIVE.match(line)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


@dataclass
class Pickaxe:
    """The result of a pickaxe search, with its own reliability attached.

    `saturated` is the important field. It means the search hit its commit
    limit, so the oldest commit found is merely the oldest one *looked at* — it
    may itself be a modification, with the true introduction further back. That
    is precisely the last-touch-as-introduction error, and it must not be
    reported at `verified`.
    """

    commit: Commit | None
    saturated: bool = False


def find_introducing_commit(
    repo: Repository,
    path: str,
    needle: str,
    *,
    max_count: int = 40,
    timeout: float = DEFAULT_TIMEOUT,
) -> Pickaxe:
    """The commit that introduced `needle` into `path`.

    `git log -S` reports commits where the *occurrence count* of the string
    changed; the last such commit in reverse-chronological order is the one that
    added it. Always pathspec-limited — a bare pickaxe walks the entire history
    diffing every commit, and `--pickaxe-regex` is avoided because it defeats the
    changed-path Bloom filters.

    The bound is what makes this safe to run per-symbol, and also what makes the
    answer conditional: `--max-count` truncates from the *recent* end, so a
    saturated search cannot see the introduction. Asking for one extra record is
    how that case is detected rather than assumed away.
    """
    raw = _git(
        repo,
        [
            "log",
            f"--max-count={max_count + 1}",
            "-S",
            needle,
            _FORMAT,
            repo.sha,
            "--",
            path,
        ],
        timeout=timeout,
    )
    records = [_parse_commit(r) for r in raw.split("\x1e") if r.strip()]
    commits = [c for c in records if c is not None]
    if not commits:
        return Pickaxe(commit=None)
    return Pickaxe(commit=commits[-1], saturated=len(commits) > max_count)


def find_reverts(repo: Repository, path: str, *, max_count: int = 200) -> list[Commit]:
    """Commits on `path` that look like reversions.

    A reverted attempt is among the highest-value rationale signals available:
    it records something that was tried and rejected, which no amount of reading
    the current code can recover.
    """
    raw = _git(
        repo,
        ["log", f"--max-count={max_count}", "--grep=^Revert", "-i", _FORMAT, repo.sha, "--", path],
    )
    records = [_parse_commit(r) for r in raw.split("\x1e") if r.strip()]
    return [c for c in records if c is not None]


def symbol_history(
    repo: Repository,
    ref: str,
    path: str,
    start: int,
    end: int,
    source: bytes,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_modifications: int = 5,
) -> SymbolHistory:
    """Build the evidence chain for one symbol.

    Degrades rather than fails: a shallow clone, a timeout or an unparseable
    history yields a `SymbolHistory` with a `note` explaining what is missing,
    never an exception that aborts the run. Silence about missing evidence is
    the thing to avoid — a document built without history looks exactly as
    authoritative as one built with it.
    """
    history = SymbolHistory(ref=ref)

    if repo.caps.shallow:
        history.note = "shallow clone: history unavailable"
        history.truncated = True
        return history

    lines = source.decode("utf-8", "replace").splitlines()[start - 1 : end]

    try:
        blamed = blame_lines(repo, path, start, end, timeout=timeout)
    except (GitError, TimeoutError) as exc:
        history.note = f"blame unavailable: {exc}"
        history.truncated = True
        return history

    needle = pickaxe_line(lines)
    found = Pickaxe(commit=None)
    if needle:
        try:
            found = find_introducing_commit(repo, path, needle, timeout=timeout)
        except (GitError, TimeoutError) as exc:
            history.note = f"pickaxe unavailable: {exc}"
            history.truncated = True

    if found.commit is not None:
        # Corroboration decides confidence. If blame also points at this commit,
        # two independent methods agree and the claim is verified. If only the
        # pickaxe found it, it is defensible but derived.
        #
        # A saturated search caps out at `derived` regardless of corroboration:
        # blame agreeing that this commit touched the lines says nothing about
        # whether an earlier commit introduced them, so the two methods are not
        # independent here and corroboration is not evidence.
        corroborated = found.commit.sha in blamed
        verified = corroborated and not found.saturated
        if found.saturated:
            history.truncated = True
            history.note = (
                history.note or "pickaxe hit its commit limit; earlier history not searched"
            )
        history.introduced = Evidence(
            kind="introduced",
            commit=found.commit,
            confidence="verified" if verified else "derived",
            method="pickaxe+blame" if corroborated else "pickaxe",
            lines=(start, end),
        )
    elif blamed:
        # No distinguishing content to pickaxe on, so the best available answer
        # is the commit touching the most lines — explicitly speculative, since
        # last-touch is not introduction.
        top = max(blamed.items(), key=lambda kv: kv[1])[0]
        commit = commit_info(repo, top)
        if commit is not None:
            history.introduced = Evidence(
                kind="introduced",
                commit=commit,
                confidence="speculative",
                method="blame-only (last touch, not necessarily introduction)",
                lines=(start, end),
            )

    introduced_sha = history.introduced.commit.sha if history.introduced else None
    for sha, touched in sorted(blamed.items(), key=lambda kv: -kv[1]):
        if sha == introduced_sha or len(history.modifications) >= max_modifications:
            continue
        commit = commit_info(repo, sha)
        if commit is not None:
            history.modifications.append(
                Evidence(
                    kind="modified",
                    commit=commit,
                    confidence="verified",
                    method=f"blame ({touched} lines)",
                )
            )

    issues: set[int] = set()
    for commit in history.commits:
        issues.update(commit.referenced_issues())
    history.issue_numbers = sorted(issues)

    return history
