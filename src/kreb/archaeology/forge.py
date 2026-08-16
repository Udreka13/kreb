"""Forge lookup — commits to pull requests and issues.

The architecture assumed the `gh` CLI. It is not installed on every machine
(including this one), so this talks to the REST API directly over stdlib
`urllib`, keeping archaeology free of network dependencies.

**Degradation is the design, not an afterthought.** Unauthenticated GitHub REST
allows 60 requests/hour, which is an absolute blocker for a document covering
more than a handful of symbols. Rather than fail, or silently produce a document
with no PR evidence that looks exactly as authoritative as one with it, this
reports what it could and could not reach, and the capability manifest carries
that into every rendered artifact.

Commit messages are mined for issue references regardless, so a repository with
no forge access at all still yields partial rationale.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API_ROOT = "https://api.github.com"
_TIMEOUT = 15.0

_REMOTE_PATTERNS = (
    re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$"),
)


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    merged_at: str | None = None

    def summary(self, limit: int = 600) -> str:
        text = (self.body or "").strip()
        return text[:limit] + ("…" if len(text) > limit else "")


@dataclass
class ForgeStatus:
    """What forge access was actually available, for the capability manifest."""

    available: bool = False
    authenticated: bool = False
    host: str = ""
    owner: str = ""
    repo: str = ""
    requests_made: int = 0
    rate_limited: bool = False
    reason: str = ""

    def describe(self) -> str:
        if not self.available:
            return f"forge: unavailable ({self.reason or 'no remote detected'})"
        if self.rate_limited:
            return "forge: rate-limited; PR evidence is incomplete"
        return f"forge: {'authenticated' if self.authenticated else 'anonymous (60 req/h)'}"


def parse_remote(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from an SSH or HTTPS GitHub remote."""
    cleaned = url.strip().removesuffix(".git")
    for pattern in _REMOTE_PATTERNS:
        match = pattern.search(cleaned + ("" if cleaned.endswith("/") else "/"))
        if match:
            return match.group("owner"), match.group("repo")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)/?$", cleaned)
    if match:
        return match.group(1), match.group(2)
    return None


class GitHubForge:
    """Minimal read-only GitHub client with honest failure reporting."""

    def __init__(self, owner: str, repo: str, token: str | None = None) -> None:
        self.owner = owner
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.status = ForgeStatus(
            available=True,
            authenticated=bool(self.token),
            host="github.com",
            owner=owner,
            repo=repo,
        )
        self._pr_cache: dict[str, PullRequest | None] = {}

    @classmethod
    def from_repo(cls, repo) -> GitHubForge | None:
        """Build from the repository's `origin` remote, or return None."""
        try:
            from kreb.archaeology.history import _git

            url = _git(repo, ["remote", "get-url", "origin"]).strip()
        except Exception:
            return None
        parsed = parse_remote(url)
        if parsed is None:
            return None
        return cls(*parsed)

    # -- transport ---------------------------------------------------------

    def _get(self, path: str) -> object | None:
        request = urllib.request.Request(
            f"{API_ROOT}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "kreb",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            self.status.requests_made += 1
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                self.status.rate_limited = True
                self.status.reason = "rate limit exceeded"
            elif exc.code == 404:
                return None
            else:
                self.status.reason = f"HTTP {exc.code}"
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.status.reason = f"network unavailable: {exc}"
            self.status.available = False
            return None

    # -- lookups -----------------------------------------------------------

    def pull_request_for_commit(self, sha: str) -> PullRequest | None:
        """The PR that carried a commit, if any.

        One request per commit. Batching via GraphQL's `associatedPullRequests`
        would cut ~100 lookups to one query and is the obvious next step if this
        becomes the bottleneck — but correctness first, and REST needs no schema.
        """
        if sha in self._pr_cache:
            return self._pr_cache[sha]
        if self.status.rate_limited or not self.status.available:
            return None

        payload = self._get(f"/repos/{self.owner}/{self.repo}/commits/{sha}/pulls")
        result: PullRequest | None = None
        if isinstance(payload, list) and payload:
            first = payload[0]
            result = PullRequest(
                number=first.get("number", 0),
                title=first.get("title", ""),
                body=first.get("body") or "",
                url=first.get("html_url", ""),
                merged_at=first.get("merged_at"),
            )
        self._pr_cache[sha] = result
        return result

    def issue(self, number: int) -> PullRequest | None:
        """An issue or PR by number. Issues and PRs share a namespace on GitHub."""
        if self.status.rate_limited or not self.status.available:
            return None
        payload = self._get(f"/repos/{self.owner}/{self.repo}/issues/{number}")
        if not isinstance(payload, dict) or "number" not in payload:
            return None
        return PullRequest(
            number=payload["number"],
            title=payload.get("title", ""),
            body=payload.get("body") or "",
            url=payload.get("html_url", ""),
        )


@dataclass
class ForgeEvidence:
    """Forge findings for one symbol, plus what could not be reached."""

    pull_requests: list[PullRequest] = field(default_factory=list)
    issues: list[PullRequest] = field(default_factory=list)
    status: ForgeStatus = field(default_factory=ForgeStatus)


def enrich(history, forge: GitHubForge | None) -> ForgeEvidence:
    """Attach PR and issue context to a `SymbolHistory`.

    With `forge=None` this returns empty evidence and an unavailable status —
    the document is still produced, and still says what it lacked.
    """
    if forge is None:
        return ForgeEvidence(status=ForgeStatus(available=False, reason="no forge configured"))

    evidence = ForgeEvidence(status=forge.status)
    seen: set[int] = set()

    for commit in history.commits:
        pull = forge.pull_request_for_commit(commit.sha)
        if pull is not None and pull.number not in seen:
            seen.add(pull.number)
            evidence.pull_requests.append(pull)

    for number in history.issue_numbers:
        if number in seen:
            continue
        found = forge.issue(number)
        if found is not None:
            seen.add(number)
            evidence.issues.append(found)

    return evidence
