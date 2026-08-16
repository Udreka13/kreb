"""Assembling the evidence pack a section is written from.

Two constraints shape everything here.

**Nothing credential-shaped may reach the model.** `repo/access.py` keeps whole
files out by path; this keeps excerpts clean, because a fixture with a live
token in an otherwise ordinary module would otherwise be quoted straight into a
prompt and, from there, into a document the user publishes.

**The pack must fit, and truncation must be visible.** Silently dropping the
back half of a symbol produces a section written about code the model never saw
— confidently, since nothing in the prompt says anything is missing. Every
truncation here leaves a marker in the text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kreb.archaeology.history import SymbolHistory
from kreb.doc.scrub import redact
from kreb.index.repo_index import IndexedSymbol, RepoIndex
from kreb.repo.access import Repository

# Rough characters-per-token. Only used to keep a pack inside a budget; being
# approximate is fine, being unbounded is not.
CHARS_PER_TOKEN = 4

TRUNCATION_MARKER = "\n… [truncated by kreb; this symbol is longer than shown] …\n"


@dataclass
class Excerpt:
    ref: str
    path: str
    language: str
    start_line: int
    end_line: int
    source: str
    truncated: bool = False

    def render(self) -> str:
        header = f"{self.ref}  (lines {self.start_line}-{self.end_line})"
        return f"### {header}\n```{self.language}\n{self.source}\n```"


@dataclass
class ContextPack:
    """Everything a section writer is allowed to look at."""

    question: str
    excerpts: list[Excerpt] = field(default_factory=list)
    histories: list[SymbolHistory] = field(default_factory=list)
    map_summary: str = ""
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []
        if self.map_summary:
            parts.append("## Repository map\n" + self.map_summary)
        if self.excerpts:
            parts.append("## Code\n" + "\n\n".join(e.render() for e in self.excerpts))
        if self.histories:
            parts.append("## History\n" + "\n".join(_render_history(h) for h in self.histories))
        if self.notes:
            # Stated in-band so the model can hedge rather than confabulate over
            # a gap it cannot otherwise detect.
            parts.append("## Limits of this evidence\n" + "\n".join(f"- {n}" for n in self.notes))
        return "\n\n".join(parts)

    @property
    def refs(self) -> list[str]:
        return [e.ref for e in self.excerpts]


def _render_history(history: SymbolHistory) -> str:
    lines = [f"### {history.ref}"]
    if history.introduced:
        evidence = history.introduced
        lines.append(
            f"- introduced: {evidence.commit.short} {evidence.commit.subject!r} "
            f"by {evidence.commit.author} ({evidence.confidence}, via {evidence.method})"
        )
        if evidence.commit.body.strip():
            lines.append(f"  message: {evidence.commit.body.strip()[:400]}")
    for modification in history.modifications[:3]:
        lines.append(
            f"- modified: {modification.commit.short} {modification.commit.subject!r} "
            f"({modification.confidence})"
        )
    for revert in history.reverts:
        lines.append(f"- REVERTED: {revert.commit.short} {revert.commit.subject!r}")
    if history.note:
        lines.append(f"- note: {history.note}")
    if not history.introduced and not history.modifications:
        lines.append("- no history recovered")
    return "\n".join(lines)


def excerpt_for(
    symbol: IndexedSymbol, repo: Repository, *, max_chars: int = 6000
) -> Excerpt | None:
    """Read one symbol's source, scrubbed and bounded."""
    try:
        source = repo.read(symbol.path)
    except Exception:
        return None

    lines = source.decode("utf-8", "replace").splitlines()
    body = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
    truncated = False
    if len(body) > max_chars:
        # Keep both ends: a signature without its return statement, or a body
        # without its guard clauses, misleads differently but just as badly.
        head = body[: max_chars // 2]
        tail = body[-max_chars // 2 :]
        body = head + TRUNCATION_MARKER + tail
        truncated = True

    return Excerpt(
        ref=symbol.ref,
        path=symbol.path,
        language=symbol.language,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        source=redact(body),
        truncated=truncated,
    )


def build_pack(
    *,
    question: str,
    refs: list[str],
    index: RepoIndex,
    repo: Repository,
    histories: list[SymbolHistory] | None = None,
    map_summary: str = "",
    max_tokens: int = 24_000,
) -> ContextPack:
    """Gather excerpts for `refs`, stopping before the token budget is spent.

    Symbols are added in the order given — the caller has already ranked them —
    and any that do not fit are named in `notes` rather than dropped in silence.
    """
    pack = ContextPack(question=question, map_summary=map_summary)
    budget_chars = max_tokens * CHARS_PER_TOKEN - len(map_summary)
    used = 0
    omitted: list[str] = []

    for ref in refs:
        symbol = index.resolve(ref)
        if symbol is None:
            omitted.append(f"{ref} (not in the index)")
            continue
        excerpt = excerpt_for(symbol, repo)
        if excerpt is None:
            omitted.append(f"{ref} (unreadable)")
            continue
        cost = len(excerpt.source)
        if used + cost > budget_chars and pack.excerpts:
            omitted.append(ref)
            continue
        pack.excerpts.append(excerpt)
        used += cost
        if excerpt.truncated:
            pack.notes.append(f"{ref} was truncated; the middle of it is not shown")

    for history in histories or []:
        pack.histories.append(history)
        if history.truncated or history.note:
            pack.notes.append(f"history for {history.ref} is incomplete: {history.note}")

    if omitted:
        pack.notes.append(
            "these symbols were not included and must not be described as if read: "
            + ", ".join(omitted[:12])
        )
    return pack
