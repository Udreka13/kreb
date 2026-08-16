"""The research loop: plan, write, cache, stop cleanly.

Three properties, each of which was a bug in the first design.

**The section is the unit.** Not the document. A run that fails on section 39
keeps sections 1–38, and a commit that touches one file invalidates only the
sections that read it. A document-level node made both of those impossible.

**Stopping is clean.** The budget is consulted *between* sections, so whatever
was finished is written and valid and the run is resumable. A killed run that
produced nothing is the worst available outcome — worse than an expensive one,
and much worse than a partial one that says it is partial.

**A cache hit is decided by the trace, not by the commit.** A section is reused
when the symbols it actually read are unchanged, which is why editing the README
does not regenerate a document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from kreb.archaeology.history import Commit, symbol_history
from kreb.budget.policy import Decision
from kreb.doc.schema import Capabilities, Document, Section, SectionKind
from kreb.index.repo_index import RepoIndex
from kreb.provider.metered import MeteredProvider
from kreb.repo.access import Repository
from kreb.research.context import build_pack
from kreb.research.writer import WriteResult, write_section
from kreb.store.keys import GeneratedKey, Trace, TraceEntry
from kreb.store.store import ArtifactStore, Provenance


@dataclass
class PlannedSection:
    """One unit of work, decided before any of it is written."""

    id: str
    title: str
    kind: SectionKind
    refs: list[str] = field(default_factory=list)
    why: str = ""
    parent_id: str | None = None


@dataclass
class RunReport:
    """What happened, in enough detail to explain a partial document."""

    document: Document
    written: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    failed: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    stopped_early: bool = False
    stop_reason: str = ""
    cost: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.failed and not self.skipped

    def summary(self) -> str:
        lines = [
            f"{len(self.written)} written, {len(self.reused)} reused from cache, "
            f"{len(self.failed)} failed, {len(self.skipped)} not attempted",
            f"cost: ${self.cost:.4f}",
        ]
        if self.stopped_early:
            lines.append(f"stopped early: {self.stop_reason}")
        for section_id, reasons in self.failed.items():
            lines.append(f"  {section_id}: {reasons[-1] if reasons else 'unknown'}")
        return "\n".join(lines)


def run_research(
    *,
    plan: list[PlannedSection],
    question: str,
    index: RepoIndex,
    repo: Repository,
    provider: MeteredProvider,
    store: ArtifactStore | None = None,
    capabilities: Capabilities | None = None,
    title: str = "Research document",
    map_summary: str = "",
    archaeology: bool = True,
    max_attempts: int = 3,
) -> RunReport:
    """Write every planned section, reusing what is still valid."""
    caps = capabilities or Capabilities(base_sha=index.sha)
    sections: list[Section] = []
    report = RunReport(document=Document(title=title, question=question, capabilities=caps))
    start_cost = provider.ledger.total()
    revert_cache: dict[str, list[Commit]] = {}

    for position, planned in enumerate(plan):
        decision = provider.budget.decide(provider.ledger, phase=provider.phase)
        if decision.stop:
            # Everything from here on is deliberately not attempted, and named
            # as such — a document that is quietly shorter than it should be is
            # indistinguishable from one that had nothing more to say.
            report.stopped_early = True
            report.stop_reason = decision.reason
            report.skipped = [p.id for p in plan[position:]]
            break

        result = _section(
            planned=planned,
            question=question,
            index=index,
            repo=repo,
            provider=provider,
            store=store,
            map_summary=map_summary,
            archaeology=archaeology,
            max_attempts=max_attempts,
            revert_cache=revert_cache,
            report=report,
        )
        if result is not None:
            sections.append(result)
            # Rebuilt after every section so a crash leaves a readable document
            # rather than an empty one.
            report.document = Document(
                title=title, question=question, capabilities=caps, sections=tuple(sections)
            )

    report.cost = provider.ledger.total() - start_cost
    return report


def _section(
    *,
    planned: PlannedSection,
    question: str,
    index: RepoIndex,
    repo: Repository,
    provider: MeteredProvider,
    store: ArtifactStore | None,
    map_summary: str,
    archaeology: bool,
    max_attempts: int,
    revert_cache: dict,
    report: RunReport,
) -> Section | None:
    histories = []
    if archaeology and planned.kind == "rationale":
        # Only rationale sections need history: structure sections are answered
        # by the code in front of them, and a pickaxe per symbol is the most
        # expensive thing in the pipeline that costs no money.
        for ref in planned.refs[:6]:
            symbol = index.resolve(ref)
            if symbol is None:
                continue
            try:
                histories.append(
                    symbol_history(
                        repo,
                        ref,
                        symbol.path,
                        symbol.start_line,
                        symbol.end_line,
                        repo.read(symbol.path),
                        revert_cache=revert_cache,
                    )
                )
            except Exception:
                continue

    pack = build_pack(
        question=question,
        refs=planned.refs,
        index=index,
        repo=repo,
        histories=histories,
        map_summary=map_summary,
    )

    def generate():
        result = write_section(
            section_id=planned.id,
            title=planned.title,
            kind=planned.kind,
            pack=pack,
            index=index,
            provider=provider,
            max_attempts=max_attempts,
            parent_id=planned.parent_id,
        )
        if not result.ok:
            raise _SectionFailed(result)

        trace = Trace(
            entries=tuple(
                TraceEntry(ref=ref, text_hash=index.symbols[ref].text_hash)
                for ref in pack.refs
                if ref in index.symbols
            )
        )
        provenance = Provenance(
            kind="section",
            key="",
            model_id=provider.model_for("research"),
            validation_attempts=result.attempts,
            usage_cost=result.cost,
        )
        return result.section.model_dump_json().encode("utf-8"), trace, provenance

    if store is None:
        try:
            result = write_section(
                section_id=planned.id,
                title=planned.title,
                kind=planned.kind,
                pack=pack,
                index=index,
                provider=provider,
                max_attempts=max_attempts,
                parent_id=planned.parent_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            report.failed[planned.id] = [str(exc)]
            return None
        return _record(planned, result, report)

    key = GeneratedKey(
        kind="section",
        node_id=planned.id,
        brief=planned.title,
        prompt_hash=_prompt_hash(pack, planned),
        model_id=provider.model_for("research"),
    )
    try:
        data, from_cache = store.materialize_generated(key, index.current_hashes(), generate)
    except _SectionFailed as failure:
        report.failed[planned.id] = failure.result.rejections
        return None

    section = Section.model_validate(json.loads(data.decode("utf-8")))
    if from_cache:
        report.reused.append(planned.id)
        # Recorded as a zero-cost row so a near-free re-run reads as reuse
        # rather than as a run that did nothing.
        from kreb.provider.types import Request

        provider.record_cache_hit(Request(messages=(), unit=planned.id))
    else:
        report.written.append(planned.id)
    return section


class _SectionFailed(Exception):
    def __init__(self, result: WriteResult) -> None:
        super().__init__("section could not be written")
        self.result = result


def _record(planned: PlannedSection, result: WriteResult, report: RunReport) -> Section | None:
    if result.ok:
        report.written.append(planned.id)
        return result.section
    report.failed[planned.id] = result.rejections
    return None


def _prompt_hash(pack, planned: PlannedSection) -> str:
    """Identity of the request, so a changed evidence pack is a different node."""
    from hashlib import sha256

    payload = json.dumps(
        {"refs": sorted(pack.refs), "kind": planned.kind, "title": planned.title},
        sort_keys=True,
    )
    return sha256(payload.encode()).hexdigest()[:32]
