"""Two-host narration: a question-asker and an answerer, over the same beats.

The second consumer of `beats`, and a sibling of `render/narration.py` rather
than a layer on top of it. Both descend from the plan and share not one
sentence, which is the same edge direction the whole audio path is built on.

Why two voices at all: a monologue that obeys the self-containment rule reads as
a well-ordered *list*. Eighteen true statements in a row is a reference, not
something you follow while doing the dishes. A host who asks the question the
listener is already forming supplies the connective tissue the monologue is
forbidden from having — and supplies it without the expert's lines losing their
independence, because the question is a separate turn.

That split is also the entire safety argument, and it is worth stating plainly.
Every claim in this pipeline is anchored: it comes from a section, it inherits
that section's confidence, and it may name only symbols that section cites. A
second speaker is a second mouth that can make claims nobody checked. So the
host does not get to make claims:

    **A host turn must end in a question mark.**

Positive, lexical, mechanically checkable — the same class of rule as "a
speculative segment must contain a hedge", and enforceable for the same reason
that "must not sound overconfident" is not. A host turn is also held to the
symbol allowlist of the beat it introduces, capped at one sentence, and can only
exist attached to a beat. There is no shape in the JSON for a host turn that
floats free of an expert answer, so a host cannot get the last word on anything.

The expert carries every rule the monologue narrator carried: sentence cap,
hedge-when-speculative, symbol allowlist, background signpost prepended. One
expert turn per beat, still mandatory, so the coverage invariant `beats`
enforces survives the second renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from kreb.budget.ledger import Charge
from kreb.doc.schema import Document
from kreb.index.repo_index import RepoIndex
from kreb.progress import Progress
from kreb.prose import split_sentences
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Message, Request
from kreb.render.beats import BeatsPlan, unlicensed_symbols
from kreb.render.narration import (
    _FENCE,
    BACKGROUND_PREFIX,
    HEDGES,
    MAX_SENTENCES,
    Narration,
    NarrationResult,
    Segment,
    _for_the_ear,
    has_hedge,
    speakable,
)

HOST = "host"
EXPERT = "expert"

# A host turn is one sentence. Two-sentence questions are a monologue wearing a
# question mark, and they are how a host starts making claims: the assertion
# rides in the first sentence and the question mark lands on the second.
MAX_HOST_SENTENCES = 1

# At least one real question, or this is a monologue with the speaker labels
# switched on — which would cost two voices and buy nothing.
MIN_QUESTIONS = 1


class TurnDraft(BaseModel):
    """One beat as an exchange. The host half is optional; the expert half is not.

    The optionality is the point of the shape. A host turn cannot exist without
    the expert turn it introduces, so no arrangement of this JSON produces a
    question nobody answers or a host with the last word.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order: int
    host: str = ""
    expert: str


class DialogueDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    turns: list[TurnDraft] = Field(default_factory=list)


DIALOGUE_SYSTEM = """\
You are writing a two-person conversation about a codebase, to be spoken aloud.

There are two speakers and they have different jobs.

The HOST asks. The host is smart, curious, and has not read the code. The host
says the thing the listener is thinking — including when that is a doubt, an
objection, or "that sounds like everything else in this space". The host never
explains, never asserts, and never answers. Every host turn is one sentence and
ends in a question mark. That is a hard rule: a host turn that states something
is a claim nobody checked, and it will be rejected.

The EXPERT answers. The expert has read the code and says what is actually
there. Two sentences at most, one is often better. Say the thing; do not
introduce it first.

Both speakers:
- Name only symbols the beat itself names. Adding one is a claim nobody checked.
- Speak names plainly — "the retry policy", not `RetryPolicy` spelled out.
- No markdown, no backticks, no bullets, no headings. This is speech.

Give a beat a host question when it earns one: when the point answers something
a listener would actually wonder, when it contradicts what they would assume, or
when the conversation has run several answers without a breath. Do not put a
question on every beat — a metronome of questions is worse than none.

Where a beat is marked HEDGE REQUIRED, the expert's line must sound uncertain
out loud — "probably", "likely", "appears to", "may", "seems". Uncertainty a
listener cannot hear is not uncertainty. Hedge in the answer, not the question.

Return JSON: {"turns": [{"order": <beat order>, "host": "...", "expert": "..."}]}
Omit "host" or leave it empty on beats that do not need a question.
"""


def dialogue_user_prompt(plan: BeatsPlan) -> str:
    parts = [f"Document: {plan.title}"]
    if plan.question:
        parts.append(f"The question this answers: {plan.question}")
        parts.append(
            "The host opens with that question and it is already spoken — "
            "do not ask it again."
        )
    parts.append("")
    parts.append("Beats, in order:")
    for beat in plan.beats:
        flags = []
        if beat.hedge_required:
            flags.append("HEDGE REQUIRED")
        if beat.prefix_required:
            flags.append("background — a signpost is added automatically, do not write one")
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        parts.append(f"{beat.order}. ({beat.section_title}) {beat.key_point}{suffix}")
    return "\n".join(parts)


def write_dialogue(
    plan: BeatsPlan,
    index: RepoIndex,
    provider: MeteredProvider,
    *,
    document: Document | None = None,
    role: str = "narrate",
    max_attempts: int = 3,
    progress: Progress | None = None,
) -> NarrationResult:
    """Expand a beat plan into a two-speaker script.

    Mirrors `write_narration` turn for turn — generate, evaluate, charge in
    `finally`, retry with the rejections appended — because the retry loop is
    where cost accounting lives and a second copy that drifts is a second
    ledger that under-counts.
    """
    base_user = dialogue_user_prompt(plan)
    rejections: list[str] = []
    spent_before = provider.ledger.total(phase=provider.phase)

    for attempt in range(1, max_attempts + 1):
        provider.budget.guard(provider.ledger, phase=provider.phase)

        user = base_user + (_retry_suffix(rejections[-4:]) if rejections else "")
        completion = provider.inner.complete(
            Request(
                messages=(Message("system", DIALOGUE_SYSTEM), Message("user", user)),
                role=role,  # type: ignore[arg-type]
                unit="dialogue",
                response_format={"type": "json_object"},
            )
        )

        narration, failures = None, []
        try:
            narration, failures = _evaluate(completion.text, plan, index, document)
        except BaseException:
            failures = ["evaluation raised while checking this dialogue"]
            raise
        finally:
            provider.ledger.charge(
                Charge.from_usage(
                    phase=provider.phase,
                    unit="dialogue",
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
                id="dialogue",
                attempt=attempt,
                failed=bool(failures),
                cost=completion.usage.cost,
                reason="; ".join(failures)[:200],
            )

        if not failures:
            return NarrationResult(
                narration=narration,
                attempts=attempt,
                rejections=rejections,
                cost=provider.ledger.total(phase=provider.phase) - spent_before,
            )
        rejections.extend(failures)

    return NarrationResult(
        narration=None,
        attempts=max_attempts,
        rejections=rejections,
        cost=provider.ledger.total(phase=provider.phase) - spent_before,
    )


def _evaluate(
    text: str, plan: BeatsPlan, index: RepoIndex, document: Document | None
) -> tuple[Narration | None, list[str]]:
    try:
        draft = DialogueDraft.model_validate_json(_FENCE.sub("", text).strip())
    except Exception as exc:
        return None, [f"output could not be parsed: {exc}"]

    turns = {turn.order: turn for turn in draft.turns}
    sections = {s.id: s for s in document.sections} if document is not None else {}
    failures: list[str] = []
    segments: list[Segment] = []
    questions = 0

    for beat in plan.beats:
        turn = turns.get(beat.order)
        answer = speakable(turn.expert.strip()) if turn else ""
        if not answer:
            failures.append(f"beat {beat.order} got no answer from the expert")
            continue

        section = sections.get(beat.section_id)
        question = speakable(turn.host.strip()) if turn else ""

        if question:
            problem = _check_question(question, beat.order, section, index)
            if problem:
                failures.append(problem)
                continue
            questions += 1

        count = len(split_sentences(answer))
        if count > MAX_SENTENCES:
            failures.append(
                f"beat {beat.order}: the expert takes {count} sentences; at most "
                f"{MAX_SENTENCES}, because one turn is one scene"
            )
            continue

        if beat.hedge_required and not has_hedge(answer):
            failures.append(
                f"beat {beat.order} comes from a speculative section but the expert "
                "states it flatly; say it with one of: "
                + ", ".join(h.strip() for h in HEDGES[:6])
            )
            continue

        if section is not None:
            stray = unlicensed_symbols(answer, section, index)
            if stray:
                failures.append(
                    f"beat {beat.order}: the expert names "
                    f"{', '.join(f'`{s}`' for s in sorted(stray))}, which its section "
                    "does not cite; say it without them"
                )
                continue

        # Prepended, never requested — a model asked for it forgets it on beat
        # nineteen. On the expert, because the expert is who makes the claim.
        if beat.prefix_required:
            answer = f"{BACKGROUND_PREFIX} {answer[0].lower() + answer[1:]}"

        if question:
            segments.append(
                Segment(
                    id=f"n{beat.order:03d}q",
                    section_id=beat.section_id,
                    beat_order=beat.order,
                    text=question,
                    confidence=beat.confidence,
                    kind=beat.kind,
                    role="question",
                    speaker=HOST,
                )
            )
        segments.append(
            Segment(
                id=f"n{beat.order:03d}",
                section_id=beat.section_id,
                beat_order=beat.order,
                text=answer,
                confidence=beat.confidence,
                kind=beat.kind,
                speaker=EXPERT,
            )
        )

    if not failures and questions < MIN_QUESTIONS:
        failures.append(
            "the host never asked anything, which makes this a monologue with "
            "speaker labels; give at least one beat a real question"
        )

    if failures:
        return None, failures

    return (
        Narration(
            title=plan.title,
            question=plan.question,
            base_sha=plan.base_sha,
            segments=tuple(_opening(plan) + segments + _closing(document)),
        ),
        [],
    )


def _check_question(question: str, order: int, section, index: RepoIndex) -> str:
    """The host's three rules, or "" if the turn is clean.

    Returned as a string rather than raised or appended in place so the caller
    keeps one failure per beat: a host turn that breaks two rules should not
    push the expert's own failure off the retry prompt.
    """
    if not question.rstrip().endswith("?"):
        return (
            f"beat {order}: the host says {question[:60]!r}, which is a statement. "
            "The host only asks — every host turn ends in a question mark, because "
            "a host who states things is making claims nobody checked"
        )
    count = len(split_sentences(question))
    if count > MAX_HOST_SENTENCES:
        return (
            f"beat {order}: the host takes {count} sentences; a question is one. "
            "Anything before the question mark is an assertion in disguise"
        )
    if section is not None:
        stray = unlicensed_symbols(question, section, index)
        if stray:
            return (
                f"beat {order}: the host names "
                f"{', '.join(f'`{s}`' for s in sorted(stray))}, which the beat's "
                "section does not cite; ask it without them"
            )
    return ""


def _opening(plan: BeatsPlan) -> list[Segment]:
    """The hook, computed rather than written.

    The host asks the document's own question verbatim and the expert says what
    kind of answer is coming — not the answer itself. That restraint is
    structural: a computed opening cannot be checked against a section, so
    anything it asserted would be the one unanchored claim in the file.
    """
    opener = plan.question.strip() or f"What is {plan.title}?"
    if not opener.endswith("?"):
        opener += "?"
    # The title is deliberately not repeated back. `kreb doc` builds titles as
    # "<repo>: <question>", so quoting it here makes the expert's first act
    # reading the host's line back to them — and, when the question ends in a
    # question mark, doing it with a stray "?." in the middle of a sentence.
    reply = (
        "Everything here comes from reading the code at one commit. "
        "Where it is not sure, it says so."
    )
    return [
        Segment(
            id="n000-open-q",
            section_id="",
            beat_order=-1,
            text=speakable(opener),
            confidence="verified",
            kind="overview",
            role="opening",
            speaker=HOST,
        ),
        Segment(
            id="n000-open",
            section_id="",
            beat_order=-1,
            text=speakable(reply),
            confidence="verified",
            kind="overview",
            role="opening",
            speaker=EXPERT,
        ),
    ]


def _closing(document: Document | None) -> list[Segment]:
    """The caveats, asked for and then given.

    The host's prompt is computed and fixed. On the page these sit in a box the
    reader sees before starting; in audio, saying nothing is a claim that the
    run saw everything.
    """
    if document is None:
        return []
    warnings = list(document.capabilities.warnings())
    if not warnings:
        return []
    joined = " ".join(_for_the_ear(w).rstrip(".") + "." for w in warnings)
    return [
        Segment(
            id="n999-close-q",
            section_id="",
            beat_order=10**6,
            text="Before we finish, is there anything this run could not see?",
            confidence="verified",
            kind="overview",
            role="closing",
            speaker=HOST,
        ),
        Segment(
            id="n999-close",
            section_id="",
            beat_order=10**6,
            text=speakable(joined),
            confidence="verified",
            kind="overview",
            role="closing",
            speaker=EXPERT,
        ),
    ]


def _retry_suffix(rejections: list[str]) -> str:
    return "\n\nYour last attempt was rejected:\n" + "\n".join(f"- {r}" for r in rejections)


def transcript(narration: Narration) -> str:
    """The script with speaker labels, for reading rather than hearing."""
    return "\n\n".join(f"{s.speaker.upper()}: {s.text}" for s in narration.segments)
