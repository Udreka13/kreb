"""Git and forge archaeology — recovering *why*, with the evidence attached.

Every claim this package produces carries the method that found it and a
confidence tier, because the difference between "the commit that introduced
this" and "the commit that last reformatted this" is invisible in the output
unless it is stated.
"""

from kreb.archaeology.forge import ForgeEvidence, ForgeStatus, GitHubForge, PullRequest, enrich
from kreb.archaeology.history import (
    Commit,
    Evidence,
    SymbolHistory,
    blame_lines,
    commit_info,
    find_introducing_commit,
    find_reverts,
    pickaxe_line,
    symbol_history,
)

__all__ = [
    "Commit",
    "Evidence",
    "ForgeEvidence",
    "ForgeStatus",
    "GitHubForge",
    "PullRequest",
    "SymbolHistory",
    "blame_lines",
    "commit_info",
    "enrich",
    "find_introducing_commit",
    "find_reverts",
    "pickaxe_line",
    "symbol_history",
]
