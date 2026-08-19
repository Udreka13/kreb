"""Beats — the plan the audio and video renderers share, and the prose they don't.

The v0.1 design ran `narration → storyboard`: write the script, then find
pictures for it. That edge points the wrong way and would have guaranteed the
failure it was meant to prevent — a narrator describing something the screen is
not showing. Video narration has to be written *against* known on-screen
content, which means the script cannot be the thing that comes first.

So what comes first is smaller than a script. A **beat** is one point worth
making, attached to the section it came from and ordered against its siblings.
`narration_audio` expands beats into self-contained prose. `storyboard` assigns
beats to scenes and `narration_video` writes against those scenes. Both renderers
make the same points in the same order and share not one sentence.

**The model selects and orders; it never authors the flags.** `confidence` and
`kind` are copied off the source section, and `hedge_required` and
`prefix_required` are computed from those — properties, not fields, so no caller
can set them either. This is the same invariant as `research/draft.py`'s "the
model names symbols but never authors an `Anchor`", and for the same reason: the
downstream validator that enforces hedging is only sound if the thing it checks
against could not have been written by the thing it is checking.

The coverage rule is the one that earns its keep. Without it a model quietly
drops the two sections it found hardest to summarize, and the audio is shorter,
smoother, and missing the part you wanted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from kreb.budget.ledger import Charge
from kreb.doc.schema import Confidence, Document, Section, SectionKind
from kreb.doc.validate import all_anchors, indexed_identifiers
from kreb.index.repo_index import RepoIndex
from kreb.progress import Progress
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Message, Request
from kreb.render.shape import Shape

# A section long enough to need summarizing is also long enough to blow the
# context on a twenty-section document. The model is choosing what to say about
# a section, not re-reading it, so the opening is the part that matters.
BODY_BUDGET = 1200

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Beat:
    """One point to make, and where it came from."""

    section_id: str
    section_title: str
    key_point: str
    confidence: Confidence
    kind: SectionKind
    order: int

    @property
    def hedge_required(self) -> bool:
        """Whether narration of this beat must carry an audible hedge.

        A property rather than a field, deliberately. As a field it would be
        settable — by a model, by a caller building a `Beat` from a dict, by a
        future renderer that found the flag inconvenient. The hedge validator
        downstream is only meaningful if the requirement is derived from the
        section every time it is read.
        """
        return self.confidence == "speculative"

    @property
    def prefix_required(self) -> bool:
        """Background beats describe somebody else's library, and a listener
        has no way to see the `background` tag that a reader gets for free."""
        return self.kind == "background"


def document_digest(document: Document) -> str:
    """A fingerprint of the exact document a plan was built from.

    `base_sha` is not enough and neither is the title: three documents answering
    three questions about one repository share a commit, and a title is a string
    a caller can pass with `--title`. Reuse keyed on anything weaker than the
    document's own content is how `kreb audio` on your second document narrates
    the first one's beats.
    """
    return hashlib.sha256(document.to_json().encode("utf-8")).hexdigest()[:16]


@dataclass
class BeatsPlan:
    """An ordered plan, with the document it was built from."""

    title: str
    question: str
    base_sha: str
    beats: tuple[Beat, ...] = ()
    doc_digest: str = ""

    def matches(self, document: Document) -> bool:
        """Whether this plan was built from exactly this document.

        An empty digest is a plan written before digests existed; treat it as a
        mismatch rather than a match, because regenerating costs a few cents and
        narrating the wrong document costs the whole artifact.
        """
        return bool(self.doc_digest) and self.doc_digest == document_digest(document)

    def for_section(self, section_id: str) -> tuple[Beat, ...]:
        return tuple(b for b in self.beats if b.section_id == section_id)

    @property
    def sections(self) -> tuple[str, ...]:
        """Section ids in beat order, each appearing once."""
        seen: list[str] = []
        for beat in self.beats:
            if beat.section_id not in seen:
                seen.append(beat.section_id)
        return tuple(seen)


@dataclass
class BeatsResult:
    plan: BeatsPlan | None
    attempts: int = 0
    rejections: list[str] = field(default_factory=list)
    cost: float = 0.0

    @property
    def ok(self) -> bool:
        return self.plan is not None


class BeatDraft(BaseModel):
    """What the model returns for one beat. Note what is absent: every flag."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section_id: str
    key_point: str


class BeatsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    beats: list[BeatDraft] = Field(default_factory=list)


# -- what a narration is allowed to name ------------------------------------


def allowed_symbols(section: Section, index: RepoIndex) -> set[str]:
    """Symbol names this section's narration may use.

    Drawn from what the section actually cites — each anchor's qualname, the
    trailing component of it, the file's stem — and from what the section itself
    already says. A narrator saying `resolve` about a section anchored on
    `RepoIndex.resolve` is describing the cited code; one who reaches for a
    symbol the section never mentioned is describing code nobody checked.

    The section's own prose counts, and the first real run is why. A section
    titled `build_index`, anchored on `build_index`, opened with "it walks every
    file and produces a `RepoIndex`" — and a beat repeating that was rejected for
    naming `RepoIndex`. The symbol was Gate A's problem when the section was
    written and it passed; re-litigating it here does not make the document
    safer, it just stops the narration from saying what the document says.
    """
    names: set[str] = set()
    names.update(indexed_identifiers(section.body, index))
    names.update(indexed_identifiers(section.title, index))
    for anchor in all_anchors(section):
        names.add(anchor.qualname)
        names.update(part for part in anchor.qualname.split(".") if part)
        path = anchor.path
        names.add(path)
        stem = path.rpartition("/")[2]
        names.add(stem)
        names.add(stem.partition(".")[0])
    for item in section.evidence:
        if not item.is_external:
            ref = item.ref
            names.add(ref)
            names.add(ref.partition("#")[2])
            names.update(part for part in ref.partition("#")[2].split(".") if part)
    return {name for name in names if name}


def unlicensed_symbols(text: str, section: Section, index: RepoIndex) -> set[str]:
    """Real symbols named in `text` that this section never cited.

    This is an absence check, which the pipeline is otherwise careful not to
    make — "does not sound like documentation" is unenforceable. This one is
    enforceable because it is set containment over a finite index, not a
    judgement: either the token names an indexed symbol outside the section's
    citations or it does not.
    """
    return indexed_identifiers(text, index) - allowed_symbols(section, index)


# -- planning ---------------------------------------------------------------

BEATS_SYSTEM = """\
You are planning the spoken version of a technical document about a codebase.

A *beat* is one point worth making out loud — a single idea, stated as a short
declarative sentence. It is not a script: you are choosing what gets said and in
what order, not writing the words a narrator will speak.

Rules:
- Every section of the document must get at least one beat. A section you found
  hard to summarize is usually the one worth hearing about.
- Aim for the beat count you are given, by drawing more points out of each
  section rather than by repeating one. A section that describes a mechanism has
  a beat for what it does, one for how, one for the case that forced the design,
  one for what it costs. Padding is worse than brevity — if the material is not
  there, plan fewer.
- Order beats so a listener who cannot scroll back still follows: what the thing
  is, then how it works, then why it was built that way.
- You may reorder freely across sections. Document order is for readers.
- Name only symbols the section itself cites. If a point needs a symbol the
  section never mentions, it is not a point this section supports.
- Do not hedge, qualify, or add caveats. Confidence is attached automatically
  from the section, and adding your own will duplicate it.

Return JSON: {"beats": [{"section_id": "...", "key_point": "..."}, ...]}
"""


def beats_user_prompt(document: Document, shape: Shape | None = None) -> str:
    """The document as a menu of sections to draw beats from."""
    parts = [
        f"Document: {document.title}",
        f"Question it answers: {document.question}" if document.question else "",
    ]
    if shape is not None:
        # The beat count is where length is really set. Eight beats is five
        # minutes no matter what the narration prompt asks for.
        parts += [
            "",
            f"Plan about {shape.beats} beats — this becomes roughly "
            f"{shape.minutes} minutes of audio for {shape.audience}. "
            f"Choose points that let the expert get into {shape.detail}.",
        ]
    parts += ["", "Sections:"]
    for section in document.sections:
        body = section.body.strip()
        if len(body) > BODY_BUDGET:
            body = body[:BODY_BUDGET].rsplit(" ", 1)[0] + " …"
        cited = ", ".join(a.ref for a in all_anchors(section)) or "(none)"
        parts += [
            "",
            f"### {section.id} — {section.title}",
            f"kind: {section.kind} · confidence: {section.confidence}",
            f"cites: {cited}",
            "",
            body,
        ]
    return "\n".join(parts)


def plan_beats(
    document: Document,
    index: RepoIndex,
    provider: MeteredProvider,
    *,
    role: str = "narrate",
    max_attempts: int = 3,
    shape: Shape | None = None,
    progress: Progress | None = None,
) -> BeatsResult:
    """Choose and order the points the spoken versions will make."""
    system = BEATS_SYSTEM
    base_user = beats_user_prompt(document, shape)
    rejections: list[str] = []
    spent_before = provider.ledger.total(phase=provider.phase)

    for attempt in range(1, max_attempts + 1):
        provider.budget.guard(provider.ledger, phase=provider.phase)

        user = base_user + (_retry_suffix(rejections[-4:]) if rejections else "")
        completion = provider.inner.complete(
            Request(
                messages=(Message("system", system), Message("user", user)),
                role=role,  # type: ignore[arg-type]
                unit="beats",
                response_format={"type": "json_object"},
            )
        )

        plan, failures = None, []
        try:
            plan, failures = _evaluate(completion.text, document, index)
        except BaseException:
            failures = ["evaluation raised while checking this plan"]
            raise
        finally:
            provider.ledger.charge(
                Charge.from_usage(
                    phase=provider.phase,
                    unit="beats",
                    role=role,
                    model=completion.model,
                    usage=completion.usage,
                    attempt=attempt,
                    failed=bool(failures),
                )
            )

        if progress is not None:
            progress.emit(
                "attempt",
                id="beats",
                attempt=attempt,
                failed=bool(failures),
                cost=completion.usage.cost,
                reason="; ".join(failures)[:200],
            )

        if not failures:
            return BeatsResult(
                plan=plan,
                attempts=attempt,
                rejections=rejections,
                cost=provider.ledger.total(phase=provider.phase) - spent_before,
            )
        rejections.extend(failures)

    return BeatsResult(
        plan=None,
        attempts=max_attempts,
        rejections=rejections,
        cost=provider.ledger.total(phase=provider.phase) - spent_before,
    )


def _evaluate(
    text: str, document: Document, index: RepoIndex
) -> tuple[BeatsPlan | None, list[str]]:
    """Parse one plan and check it mechanically. Nothing here is a taste call."""
    try:
        draft = BeatsDraft.model_validate_json(_FENCE.sub("", text).strip())
    except Exception as exc:
        return None, [f"output could not be parsed: {exc}"]

    if not draft.beats:
        return None, ["the plan contained no beats"]

    by_id = {s.id: s for s in document.sections}
    failures: list[str] = []
    beats: list[Beat] = []

    for item in draft.beats:
        section = by_id.get(item.section_id)
        if section is None:
            failures.append(
                f"`{item.section_id}` is not a section of this document; "
                f"use one of: {', '.join(by_id)}"
            )
            continue
        point = item.key_point.strip()
        if not point:
            failures.append(f"a beat for `{item.section_id}` had an empty key point")
            continue
        stray = unlicensed_symbols(point, section, index)
        if stray:
            failures.append(
                f"the beat for `{item.section_id}` names "
                f"{', '.join(f'`{s}`' for s in sorted(stray))}, which that section "
                "does not cite; make the point using only what it cites, or drop it"
            )
            continue
        beats.append(
            Beat(
                section_id=section.id,
                section_title=section.title,
                key_point=point,
                confidence=section.confidence,
                kind=section.kind,
                order=len(beats),
            )
        )

    covered = {b.section_id for b in beats}
    missing = [s.id for s in document.sections if s.id not in covered]
    if missing:
        failures.append(
            f"these sections got no beat: {', '.join(missing)}. Every section "
            "must be represented, including ones that were hard to summarize"
        )

    if failures:
        return None, failures
    return (
        BeatsPlan(
            title=document.title,
            question=document.question,
            base_sha=document.capabilities.base_sha,
            beats=tuple(beats),
            doc_digest=document_digest(document),
        ),
        [],
    )


def _retry_suffix(reasons: list[str]) -> str:
    return "\n\nThe previous attempt was rejected:\n" + "\n".join(f"- {r}" for r in reasons)


def to_json(plan: BeatsPlan) -> str:
    """Serialize a plan. The derived flags are written out for a reader's
    benefit but never read back — `Beat` recomputes them from `confidence`."""
    return json.dumps(
        {
            "title": plan.title,
            "question": plan.question,
            "base_sha": plan.base_sha,
            "doc_digest": plan.doc_digest,
            "beats": [
                {
                    "section_id": b.section_id,
                    "section_title": b.section_title,
                    "key_point": b.key_point,
                    "confidence": b.confidence,
                    "kind": b.kind,
                    "order": b.order,
                    "hedge_required": b.hedge_required,
                    "prefix_required": b.prefix_required,
                }
                for b in plan.beats
            ],
        },
        indent=2,
    )


def from_json(text: str) -> BeatsPlan:
    data = json.loads(text)
    return BeatsPlan(
        title=data["title"],
        question=data.get("question", ""),
        base_sha=data.get("base_sha", ""),
        doc_digest=data.get("doc_digest", ""),
        beats=tuple(
            Beat(
                section_id=b["section_id"],
                section_title=b.get("section_title", ""),
                key_point=b["key_point"],
                confidence=b["confidence"],
                kind=b["kind"],
                order=b.get("order", i),
            )
            for i, b in enumerate(data.get("beats", ()))
        ),
    )
