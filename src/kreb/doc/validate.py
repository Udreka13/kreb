"""Document validation — the mechanical half of trust.

What these rules actually prove is narrower than it looks, and saying so is part
of the design. **Anchor validation proves a symbol exists. It does not prove the
symbol supports the claim.** A model that cites a real function and lies about
what it does passes every rule here, cleanly. That gap is real, it is not
closable by any structural rule, and Gate A reports it as a manual audit rather
than implying coverage that does not exist.

One rule was deliberately *removed* from the v0.1 design and must not come back:
a lexical "repo-scoped verb" linter on background sections. It fails three ways,
but the killer is that the generator runs behind a retry-and-validate loop — so
the model rewrites until the check passes, laundering the claim into a blessed
form and stamping it valid. **A cheap negative-semantic check under retry
pressure is worse than no check**, because it manufactures confidence. Every
rule below is either positive (this must be present) or purely structural.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from kreb.doc.schema import Anchor, Document, Section, Staleness
from kreb.doc.scrub import findings as secret_findings
from kreb.index.repo_index import RepoIndex

Severity = Literal["fail", "warn"]

# Backticked spans, plus bare tokens that look like code by convention.
_BACKTICKED = re.compile(r"`([^`\n]{1,120})`")
_CODEY = re.compile(r"\b(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    section_id: str
    message: str
    ref: str = ""

    def __str__(self) -> str:
        where = f"{self.section_id}" + (f" [{self.ref}]" if self.ref else "")
        return f"{self.severity.upper()} {self.rule}: {where} — {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    # ref -> staleness, for every anchor in the document.
    staleness: dict[str, Staleness] = field(default_factory=dict)
    # ref -> where it moved to, when staleness is `moved`.
    moved: dict[str, str] = field(default_factory=dict)
    anchor_total: int = 0
    anchor_resolved: int = 0
    verified_sections: int = 0
    verified_with_anchor: int = 0

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def add(self, rule: str, severity: Severity, section: str, message: str, ref: str = "") -> None:
        self.findings.append(Finding(rule, severity, section, message, ref))


def anchor_staleness(anchor: Anchor, index: RepoIndex) -> tuple[Staleness, str | None]:
    """Classify an anchor against the current index.

    Four states, not two. `moved` in particular must be distinguished from
    `broken`: a symbol whose recorded content exists at a different ref was
    relocated, and reporting that as a dangling anchor would be both wrong and
    actively unhelpful — the claim is still true, the address changed.
    """
    symbol = index.resolve(anchor.ref)
    if symbol is not None:
        return ("fresh" if symbol.text_hash == anchor.text_hash else "stale"), None
    destination = index.moved_to(anchor.ref, anchor.text_hash)
    if destination is not None:
        return "moved", destination
    return "broken", None


def identifiers_in(text: str) -> set[str]:
    """Tokens in prose that are plausibly repository symbols.

    Backticks are the strong signal; the conventional-casing fallback catches
    the common case of a model writing `RetryPolicy` unquoted. Both are then
    looked up in the index, so a token that is not a real symbol costs nothing.
    """
    found = {m.group(1).strip() for m in _BACKTICKED.finditer(text)}
    # Strip call parentheses and attribute chains down to lookup-able names.
    expanded: set[str] = set()
    for token in found:
        token = token.split("(")[0].strip()
        if token:
            expanded.add(token)
            expanded.add(token.rpartition(".")[2])
    expanded.update(m.group(0) for m in _CODEY.finditer(text))
    return {t for t in expanded if t and not t.isspace()}


def indexed_identifiers(text: str, index: RepoIndex) -> set[str]:
    """The subset of `identifiers_in` that names a real symbol in this repo."""
    return {token for token in identifiers_in(text) if index.by_name.get(token)}


def validate(doc: Document, index: RepoIndex) -> Report:
    """Run every mechanical rule over a document."""
    report = Report()
    for section in doc.sections:
        _check_anchors(section, index, report)
        _check_confidence(section, report)
        _check_repo_claims(section, index, report)
        _check_background(section, index, report)
        _check_secrets(section, report)
        _check_diagram(section, report)
    _check_manifest_consistency(doc, report)
    return report


# -- rules -----------------------------------------------------------------


def _check_anchors(section: Section, index: RepoIndex, report: Report) -> None:
    """Rules 1 and 3: misplaced and fabricated anchors are *different* failures.

    A real symbol cited at the wrong path passes one and fails the other, and
    they have different causes — a stale path versus an invented name — so
    collapsing them into "bad anchor" throws away the diagnosis.
    """
    anchors = list(section.anchors) + list(section.diagram.anchors if section.diagram else [])
    for anchor in anchors:
        report.anchor_total += 1
        status = index.anchor_status(anchor.ref)
        state, destination = anchor_staleness(anchor, index)
        report.staleness[anchor.ref] = state
        if destination:
            report.moved[anchor.ref] = destination

        if status == "resolved":
            report.anchor_resolved += 1
            if state == "stale":
                report.add(
                    "stale_anchor",
                    "warn",
                    section.id,
                    "the cited symbol changed since this was written",
                    anchor.ref,
                )
            continue

        if state == "moved":
            report.anchor_resolved += 1
            report.add(
                "moved_anchor",
                "warn",
                section.id,
                f"symbol moved to {destination}; the claim may still hold",
                anchor.ref,
            )
        elif status == "misplaced":
            matches = index.find_by_name(anchor.qualname) or index.find_by_name(
                anchor.qualname.rpartition(".")[2]
            )
            elsewhere = matches[0].ref if matches else "?"
            report.add(
                "misplaced_anchor",
                "fail",
                section.id,
                f"symbol does not exist at the cited path; found at {elsewhere}",
                anchor.ref,
            )
        elif status == "ambiguous":
            report.add(
                "ambiguous_anchor",
                "warn",
                section.id,
                "several symbols share this name; the anchor cannot be resolved to one",
                anchor.ref,
            )
        else:
            report.add(
                "fabricated_anchor",
                "fail",
                section.id,
                "symbol exists nowhere in the index",
                anchor.ref,
            )


def _check_confidence(section: Section, report: Report) -> None:
    """`verified` must be earned by a resolving anchor, not asserted.

    Confidence inflation destroys the whole trust model: if `verified` can mean
    "the model felt sure", the tier carries no information and the reader is
    better off without it.
    """
    if section.confidence != "verified":
        return
    report.verified_sections += 1
    if section.anchors:
        report.verified_with_anchor += 1
    else:
        report.add(
            "verified_without_anchor",
            "fail",
            section.id,
            "confidence is `verified` but the section carries no symbol anchor",
        )


def _check_repo_claims(section: Section, index: RepoIndex, report: Report) -> None:
    """A claim naming this repo's symbols needs evidence from this repo.

    This is the enforceable form of "library docs must not impersonate codebase
    knowledge". It is positive and mechanical — the section must *carry*
    something — and it is hard to evade, because evading it means not naming the
    repository's symbols, which is exactly the desired behaviour for a section
    that is not about the repository.
    """
    named = indexed_identifiers(section.body, index)
    if not named:
        return
    if section.repo_evidence or section.anchors:
        return
    sample = ", ".join(sorted(named)[:4])
    report.add(
        "external_only_repo_claim",
        "fail",
        section.id,
        f"names repository symbols ({sample}) with no evidence from this repository",
    )


def _check_background(section: Section, index: RepoIndex, report: Report) -> None:
    """A `background` section is about the world, not this repository.

    Reported rather than rejected: the render-time containment — a visually
    distinct block labelled "About the library, not this repo", and a mandatory
    spoken prefix in narration — is the real mitigation. Telling the reader what
    kind of statement they are looking at beats trying to detect a semantic
    property no lexical rule can decide.
    """
    if section.kind != "background":
        return
    if section.anchors:
        report.add(
            "background_cites_repo",
            "warn",
            section.id,
            "a background section carries repository anchors; it may be making "
            "claims about this repo under a label that says it is not",
            section.anchors[0].ref,
        )


def _check_secrets(section: Section, report: Report) -> None:
    """No credential-shaped content reaches a document the user may publish."""
    for name, found in secret_findings(section.body):
        report.add(
            "secret_in_body",
            "fail",
            section.id,
            f"{name} appears in the section body (…{found[-4:]})",
        )


def _check_diagram(section: Section, report: Report) -> None:
    """An `extracted` diagram must be traceable to the code it was read from."""
    diagram = section.diagram
    if diagram is None:
        return
    if not diagram.d2_source.strip():
        report.add("empty_diagram", "fail", section.id, "diagram has no source")
    if diagram.provenance == "extracted" and not diagram.anchors:
        report.add(
            "unanchored_extracted_diagram",
            "fail",
            section.id,
            "diagram claims to be extracted from the code but cites no symbols; "
            "an asserted diagram must say so",
        )


def _check_manifest_consistency(doc: Document, report: Report) -> None:
    """The manifest must not claim capabilities the document contradicts."""
    caps = doc.capabilities
    if caps.git == "shallow":
        for section in doc.sections:
            commits = [e for e in section.evidence if e.kind == "commit"]
            if commits:
                report.add(
                    "impossible_evidence",
                    "fail",
                    section.id,
                    "cites commit evidence, but the manifest reports a shallow clone "
                    "with no history available",
                    commits[0].ref,
                )
    if caps.forge in ("none", "rate_limited"):
        for section in doc.sections:
            forge_evidence = [e for e in section.evidence if e.kind in ("pull_request", "issue")]
            if forge_evidence:
                report.add(
                    "impossible_evidence",
                    "fail",
                    section.id,
                    f"cites forge evidence, but the manifest reports forge={caps.forge}",
                    forge_evidence[0].ref,
                )
