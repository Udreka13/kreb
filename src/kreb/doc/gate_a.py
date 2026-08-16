"""Gate A — mechanical factuality.

This is the regression suite for the research pipeline, and it is built now
rather than later because it is cheap now and impossible to retrofit honestly:
once a corpus of documents exists, the temptation is to set thresholds where the
current output happens to land.

**Gate A does not measure usefulness.** A document can pass every check here by
being trivially, verifiably true — "`RetryPolicy` is a class defined in
`retry.py`" — and be worth nothing. That judgement is Gate B's, it is human, and
no amount of mechanical checking substitutes for it.

Three of the PRD's seven thresholds are **not mechanically decidable** and are
reported as outstanding manual audits rather than silently omitted. An
incomplete gate that says so is trustworthy; one that reports 4/4 green while
quietly skipping the hard three is not — it is exactly the false assurance this
project exists to argue against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kreb.doc.schema import Document
from kreb.doc.validate import Report, validate
from kreb.index.repo_index import RepoIndex


@dataclass
class Check:
    name: str
    passed: bool
    measured: str
    threshold: str
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name}: {self.measured} (threshold {self.threshold})"


# Thresholds the PRD requires that no structural rule can decide. Listing them
# as data, rather than as prose in a docstring, is what keeps them visible in
# every report instead of quietly dropping out of scope.
MANUAL_AUDITS: tuple[tuple[str, str], ...] = (
    (
        "sampled `derived` claims defensible on manual audit",
        "≥90% of n=20 per repo — requires a human reading the code",
    ),
    (
        "claims typed `verified` that are actually inferred",
        "≤5% — anchor rules prove a symbol exists, never that it supports the claim",
    ),
    (
        "dependency claims checked against the pinned version",
        "100% — needs `external/`, which resolves the lockfile; not built yet",
    ),
)


@dataclass
class GateAResult:
    checks: list[Check] = field(default_factory=list)
    report: Report | None = None
    manual: tuple[tuple[str, str], ...] = MANUAL_AUDITS

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def summary(self) -> str:
        lines = [str(c) for c in self.checks]
        lines.append("")
        lines.append(f"Gate A: {'PASS' if self.passed else 'FAIL'} "
                     f"({sum(c.passed for c in self.checks)}/{len(self.checks)} mechanical checks)")
        lines.append("")
        lines.append("Not mechanically decidable — outstanding manual audits:")
        lines.extend(f"  · {name} — {threshold}" for name, threshold in self.manual)
        return "\n".join(lines)


def run(doc: Document, index: RepoIndex) -> GateAResult:
    """Run every mechanically decidable Gate A threshold over a document."""
    report = validate(doc, index)
    result = GateAResult(report=report)

    fabricated = [f for f in report.failures if f.rule == "fabricated_anchor"]
    result.checks.append(
        Check(
            name="fabricated anchors",
            passed=not fabricated,
            measured=f"{len(fabricated)}",
            threshold="0 — they look like evidence, which is what makes them the worst case",
            detail="; ".join(f.ref for f in fabricated[:5]),
        )
    )

    misplaced = [f for f in report.failures if f.rule == "misplaced_anchor"]
    result.checks.append(
        Check(
            name="misplaced anchors",
            passed=not misplaced,
            measured=f"{len(misplaced)}",
            threshold="0",
            detail="; ".join(f.ref for f in misplaced[:5]),
        )
    )

    unearned = [f for f in report.failures if f.rule == "verified_without_anchor"]
    share = (
        f"{report.verified_with_anchor}/{report.verified_sections}"
        if report.verified_sections
        else "n/a (no `verified` sections)"
    )
    result.checks.append(
        Check(
            name="`verified` claims whose anchor resolves",
            passed=not unearned,
            measured=share,
            threshold="100% — a dangling anchor is a hard fail, not a warning",
        )
    )

    external_only = [f for f in report.failures if f.rule == "external_only_repo_claim"]
    result.checks.append(
        Check(
            name="repo claims whose only evidence is external",
            passed=not external_only,
            measured=f"{len(external_only)}",
            threshold="0 — this is library documentation impersonating codebase knowledge",
            detail="; ".join(f.section_id for f in external_only[:5]),
        )
    )

    leaks = [f for f in report.failures if f.rule == "secret_in_body"]
    result.checks.append(
        Check(
            name="credential-shaped content in section bodies",
            passed=not leaks,
            measured=f"{len(leaks)}",
            threshold="0 — the user commits and publishes this output",
        )
    )

    impossible = [f for f in report.failures if f.rule == "impossible_evidence"]
    result.checks.append(
        Check(
            name="evidence the capability manifest says was unavailable",
            passed=not impossible,
            measured=f"{len(impossible)}",
            threshold="0 — a document must not contradict its own manifest",
        )
    )

    return result


def staleness_recall(doc: Document, before: RepoIndex, after: RepoIndex) -> Check:
    """The PRD's synthetic-refactor threshold, made runnable.

    Every anchor whose symbol actually changed between two indexes must be
    reported as `stale` or `moved`. A miss here is the original fatal bug — a
    section served as current after the code beneath it changed — so this is
    measured as recall against a known-changed set, not spot-checked.
    """
    from kreb.doc.validate import anchor_staleness

    changed = {
        ref
        for ref, symbol in before.symbols.items()
        if ref not in after.symbols or after.symbols[ref].text_hash != symbol.text_hash
    }
    should_flag = [a for _, a in doc.anchors() if a.ref in changed]
    flagged = [
        a for a in should_flag if anchor_staleness(a, after)[0] in ("stale", "moved", "broken")
    ]
    total = len(should_flag)
    return Check(
        name="sections flagged stale after a synthetic refactor",
        passed=len(flagged) == total,
        measured=f"{len(flagged)}/{total}" if total else "n/a (no anchors changed)",
        threshold="100%",
        detail="; ".join(a.ref for a in should_flag if a not in flagged),
    )
