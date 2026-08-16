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
from datetime import datetime

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
    r"|(?:#|//|/\*).*"  # comment lines
    # A leading `*` is a C block-comment continuation (` * Returns the count`)
    # — but it is also a pointer dereference (`*err = fmt(*addr);`), which is
    # real, distinctive content. Excluding both loses the needle on C and Go
    # bodies made of deref lines, dropping those symbols to a blame-only answer:
    # the last-touch commit, which is the one wrong answer this module must
    # never give. Operators tell them apart.
    r"|\*[^=;(){}\[\]]*"
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
    reverts: list[Evidence] = field(default_factory=list)
    issue_numbers: list[int] = field(default_factory=list)
    truncated: bool = False
    note: str = ""

    @property
    def commits(self) -> list[Commit]:
        out = [self.introduced.commit] if self.introduced else []
        out.extend(m.commit for m in self.modifications)
        out.extend(r.commit for r in self.reverts)
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


def pickaxe_candidates(source_lines: list[str], limit: int = 3) -> list[str]:
    """Distinctive lines to search history for, longest first.

    Several, not one, and that is the point. The pickaxe finds when a line's
    *exact current text* first appeared, so any line touched by a later rename
    reports that rename as the introduction — confidently, since blame agrees it
    touched the range. A neighbouring line untouched since the original commit
    gives the right answer. Searching only the longest line is a coin flip
    between them.

    A generic line (`return`, `}`, an import) is excluded because it would match
    thousands of commits, turning a bounded lookup into a full-history scan with
    a useless answer at the end of it.
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for line in source_lines:
        stripped = line.strip()
        if len(stripped) < 12 or _UNDISTINCTIVE.match(line) or stripped in seen:
            continue
        seen.add(stripped)
        candidates.append(stripped)
    candidates.sort(key=len, reverse=True)
    return candidates[:limit]


def pickaxe_line(source_lines: list[str]) -> str | None:
    """The single most distinctive line, or None. See `pickaxe_candidates`."""
    candidates = pickaxe_candidates(source_lines, limit=1)
    return candidates[0] if candidates else None


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
    # Every commit that changed the needle's occurrence count, newest first.
    # Intersecting this with the file's reverts is what makes a revert
    # attributable to *this* symbol rather than merely to the same file.
    touching: list[Commit] = field(default_factory=list)
    # Whether every needle that produced an answer produced the *same* answer.
    # Disagreement is informative: it means at least one line was rewritten
    # after the symbol was introduced, so the result is a bound, not a sighting.
    agreed: bool = True


def _is_ancestor(repo: Repository, older: str, newer: str) -> bool:
    """Whether `older` is an ancestor of `newer`, by exit code."""
    proc = subprocess.run(
        ["git", *_GIT_FLAGS, "merge-base", "--is-ancestor", older, newer],
        cwd=repo.root,
        env=_GIT_ENV,
        capture_output=True,
        check=False,
        timeout=DEFAULT_TIMEOUT,
    )
    return proc.returncode == 0


def _oldest(repo: Repository, results: list[Pickaxe]) -> Pickaxe:
    """Combine per-needle searches into the oldest defensible answer.

    Ordered by ancestry rather than by date. Author dates are the obvious
    choice and the wrong one: they are attacker- and rebase-controlled, they tie
    when several commits land in the same second, and a cherry-pick carries the
    original date onto a much later commit. `merge-base --is-ancestor` asks the
    graph, which is the thing that actually defines "earlier".
    """
    answered = [r for r in results if r.commit is not None]
    if not answered:
        return Pickaxe(commit=None, saturated=any(r.saturated for r in results))

    best = answered[0]
    for candidate in answered[1:]:
        if candidate.commit.sha == best.commit.sha:
            continue
        if _is_ancestor(repo, candidate.commit.sha, best.commit.sha):
            best = candidate
    touching: list[Commit] = []
    seen: set[str] = set()
    for result in answered:
        for commit in result.touching:
            if commit.sha not in seen:
                seen.add(commit.sha)
                touching.append(commit)
    return Pickaxe(
        commit=best.commit,
        saturated=any(r.saturated for r in results),
        touching=touching,
        agreed=len({r.commit.sha for r in answered}) == 1,
    )


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
    return Pickaxe(
        commit=commits[-1],
        saturated=len(commits) > max_count,
        touching=commits,
    )


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
    max_needles: int = 3,
    revert_cache: dict[str, list[Commit]] | None = None,
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

    # Search several needles and keep the oldest answer. A line touched by a
    # later rename dates itself to the rename; a neighbouring line untouched
    # since the original commit dates itself correctly. The introduction of the
    # symbol is bounded above by the oldest of them, never by whichever line
    # happened to be longest.
    results: list[Pickaxe] = []
    for needle in pickaxe_candidates(lines, limit=max_needles):
        try:
            results.append(find_introducing_commit(repo, path, needle, timeout=timeout))
        except (GitError, TimeoutError) as exc:
            history.note = f"pickaxe unavailable: {exc}"
            history.truncated = True
            break

    found = _oldest(repo, results)

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
        elif not found.agreed:
            # Recorded as context, deliberately *not* as a confidence penalty.
            # Disagreement cannot tell the two cases apart: a symbol introduced
            # in C1 and extended in C5 disagrees (and the oldest answer is
            # right), and a symbol whose every line was rewritten since C1 also
            # disagrees (and the oldest answer is too recent). A signal that
            # fires equally on the correct and incorrect case is not evidence,
            # and spending confidence on it would only make `verified`
            # unreachable for any symbol that was ever edited.
            history.note = history.note or (
                "the symbol was modified after it was introduced; "
                "the oldest surviving line dates it"
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

    # A reverted attempt is among the highest-value rationale signals there is:
    # it records something tried and rejected, which no amount of reading the
    # current code recovers. But `find_reverts` is scoped to the *file*, and a
    # revert elsewhere in a 900-line module says nothing about this symbol.
    # Intersecting with the pickaxe's commit set is what earns the attribution.
    if found.touching:
        try:
            file_reverts = _cached_reverts(repo, path, revert_cache)
        except (GitError, TimeoutError):
            file_reverts = []
        touching_shas = {c.sha for c in found.touching}
        for commit in file_reverts:
            if commit.sha in touching_shas:
                history.reverts.append(
                    Evidence(
                        kind="reverted",
                        commit=commit,
                        confidence="derived",
                        method="revert commit touching this symbol's content",
                    )
                )

    issues: set[int] = set()
    for commit in history.commits:
        issues.update(commit.referenced_issues())
    history.issue_numbers = sorted(issues)

    return history


def _cached_reverts(
    repo: Repository, path: str, cache: dict[str, list[Commit]] | None
) -> list[Commit]:
    """Reverts are a per-file property; a per-symbol lookup would re-scan.

    A module with forty symbols would otherwise run forty identical
    `--grep=^Revert` walks over the same path.
    """
    if cache is None:
        return find_reverts(repo, path)
    if path not in cache:
        cache[path] = find_reverts(repo, path)
    return cache[path]
