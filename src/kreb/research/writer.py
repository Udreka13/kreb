"""Writing one section: generate, resolve, validate, retry.

The retry loop here is the one place in the pipeline where a negative check sits
behind a regeneration — which is exactly the arrangement that lets a model
launder a claim into a blessed form. Two things keep it honest.

**Only mechanical rejections are fed back.** A retry reason is always something
structurally checkable: a symbol that does not resolve, malformed JSON, a
confidence level without an anchor. No rejection reason is ever "this reads like
library documentation", because that is a semantic judgement, and a model told
to avoid sounding like something will simply stop sounding like it.

**Every attempt is charged and counted.** The attempt count is carried out of
here and into the ledger, so a rule that quietly costs three generations per
section is visible as spend rather than hidden as latency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kreb.budget.ledger import Charge
from kreb.doc.schema import Section, SectionKind
from kreb.index.repo_index import RepoIndex
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Message, Request
from kreb.research.context import ContextPack
from kreb.research.draft import materialize, parse_draft
from kreb.research.prompts import (
    BACKGROUND_SYSTEM,
    SECTION_SYSTEM,
    retry_suffix,
    section_user_prompt,
)


@dataclass
class WriteResult:
    """A section, or the reasons there isn't one."""

    section: Section | None
    attempts: int = 0
    rejections: list[str] = field(default_factory=list)
    cost: float = 0.0

    @property
    def ok(self) -> bool:
        return self.section is not None


def write_section(
    *,
    section_id: str,
    title: str,
    kind: SectionKind,
    pack: ContextPack,
    index: RepoIndex,
    provider: MeteredProvider,
    role: str = "research",
    max_attempts: int = 3,
    parent_id: str | None = None,
) -> WriteResult:
    """Generate one section, retrying only on mechanically-checkable failures."""
    system = BACKGROUND_SYSTEM if kind == "background" else SECTION_SYSTEM
    base_user = section_user_prompt(
        title=title, question=pack.question, kind=kind, evidence=pack.render()
    )

    rejections: list[str] = []
    spent_before = provider.ledger.total(phase=provider.phase)

    for attempt in range(1, max_attempts + 1):
        provider.budget.guard(provider.ledger, phase=provider.phase)

        user = base_user + (retry_suffix(rejections[-4:]) if rejections else "")
        request = Request(
            messages=(Message("system", system), Message("user", user)),
            role=role,  # type: ignore[arg-type]
            unit=section_id,
            response_format={"type": "json_object"},
        )
        completion = provider.inner.complete(request)

        # The generation exists and has been billed by this point, so the
        # charge is recorded in a `finally` — a crash inside evaluation must
        # not turn a paid-for call into a free one in the ledger.
        section = None
        failures: list[str] = []
        try:
            section, failures = _evaluate(
                completion.text, section_id, title, kind, index, parent_id
            )
        except BaseException:
            failures = ["evaluation raised while checking this generation"]
            raise
        finally:
            provider.ledger.charge(
                Charge.from_usage(
                    phase=provider.phase,
                    unit=section_id,
                    role=role,
                    model=completion.model,
                    usage=completion.usage,
                    attempt=attempt,
                    failed=bool(failures),
                )
            )

        if not failures:
            return WriteResult(
                section=section,
                attempts=attempt,
                rejections=rejections,
                cost=provider.ledger.total(phase=provider.phase) - spent_before,
            )
        rejections.extend(failures)

    return WriteResult(
        section=None,
        attempts=max_attempts,
        rejections=rejections,
        cost=provider.ledger.total(phase=provider.phase) - spent_before,
    )


def _evaluate(
    text: str,
    section_id: str,
    title: str,
    kind: SectionKind,
    index: RepoIndex,
    parent_id: str | None,
) -> tuple[Section | None, list[str]]:
    """Parse and resolve one generation. Returns the section and its rejections."""
    try:
        draft = parse_draft(text)
    except (ValueError, Exception) as exc:  # pydantic raises its own
        return None, [f"output could not be parsed: {exc}"]

    if not draft.body.strip():
        return None, ["the section body was empty"]

    result = materialize(
        draft,
        section_id=section_id,
        title=title,
        kind=kind,
        index=index,
        parent_id=parent_id,
    )

    # A downgrade is a fix, not a failure: the section is usable and
    # regenerating it would cost another call to say the same thing more
    # modestly. Only unresolvable citations force another attempt.
    hard = [r for r in result.rejections if "downgraded" not in r]
    if hard:
        return None, hard

    if kind == "background" and result.section.anchors:
        return None, [
            "a background section must not cite repository symbols; "
            "leave `cites` empty and describe the library generally"
        ]

    return result.section, []
