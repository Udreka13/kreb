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

What is checked here, and what deliberately is not, is the whole design.

An earlier version made the host end every turn in a question mark, capped it at
one sentence, and demanded at least one question per script. Those are style
rules dressed as validators, and they produce exactly the output they look like
they prevent: a metronome of clipped interrogatives that no person has ever
spoken. Naturalness is not a property you can assert your way to — it comes from
the prompt, and a modern model one-shots it given a good one. Those rules are
gone.

What remains is the fabrication guard, which is a different thing entirely.
Every claim in this pipeline is anchored: it comes from a section, it inherits
that section's confidence, and it may name only symbols that section cites. A
second speaker is a second mouth, so the host is held to the same *symbol
allowlist* as the expert — set containment against the index, not a judgement
about tone. A host naming a symbol the section never cited is a fabricated
anchor with a question mark after it, and it stays rejected.

The expert carries every rule the monologue narrator carried: sentence cap,
hedge-when-speculative, symbol allowlist, background signpost prepended. One
expert turn per beat, still mandatory, so the coverage invariant `beats`
enforces survives the second renderer.

Coherence — does this sound like two people talking, does the question actually
set up the answer — is judged in `render/critique.py` and *reported*, never fed
back into this retry loop. `research/writer.py` states the reason: a rejection
reason that is a semantic judgement teaches a model to dodge the judgement.
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
You are writing a conversation between two people about a codebase, to be heard
rather than read. Write it the way people actually talk.

THE HOST has not read the code and is genuinely curious. The host says what a
listener is thinking: asks the obvious question, pushes back when something
sounds too neat, says "wait, why would you do that" when it is warranted, and
sometimes just reacts — "huh", "that's the opposite of what I expected" — before
asking anything. The host is allowed to be skeptical and allowed to be wrong.

THE EXPERT has read the code and says what is actually there. Two sentences at
most, one is often better. Answer the question that was asked, not a nearby one.

What makes this sound like people rather than a quiz:
- The host does not ask a question every time. Sometimes the expert just keeps
  going, and the host comes back in a couple of beats later. A question on every
  beat is a metronome and it is instantly recognizable as machine-written.
- Vary the shape. Not every host turn is an interrogative — "So that's why the
  hashes are split." is a real thing a person says, and the expert can answer it.
- Let the host's turn set up the answer that follows. A question the answer does
  not address is worse than no question.
- No filler that means nothing. "Great question" and "That's fascinating" are
  what this fails as. React to the substance or say nothing.

Both speakers:
- Name only symbols the beat itself names. Adding one is a claim nobody checked,
  and it applies to the host too — a question naming an invented function is
  still an invention.
- Speak names plainly — "the retry policy", not `RetryPolicy` spelled out.
- No markdown, no backticks, no bullets, no headings. This is speech.

Where a beat is marked HEDGE REQUIRED, the expert's line must sound uncertain
out loud — "probably", "likely", "appears to", "may", "seems". Uncertainty a
listener cannot hear is not uncertainty. Hedge in the answer, not the question.

Return JSON: {"turns": [{"order": <beat order>, "host": "...", "expert": "..."}]}
Omit "host" or leave it empty on beats where the expert simply continues.
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
    """The one rule the host still has, or "" if the turn is clean.

    Only the symbol allowlist. Not because the host cannot say something silly,
    but because "silly" is a judgement and this loop only rejects on things that
    are mechanically true or false. A symbol the section never cited is that:
    set containment against the index, the same check the expert gets.
    """
    if section is None:
        return ""
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
