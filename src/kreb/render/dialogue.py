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

The expert carries the rules that are about truth rather than taste:
hedge-when-speculative, symbol allowlist, background signpost prepended. One
expert turn per beat, still mandatory, so the coverage invariant `beats`
enforces survives the second renderer.

The sentence cap is not a rule here, it is a *cut*. A segment is the TTS cache
unit and the video scene unit, so it has to stay short — but a long stretch where
the expert just runs with it is one of the things that makes audio sound like a
person. So a long turn is split into scene-sized segments rather than rejected.
The words are the model's; the segmentation is ours.

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
Write a podcast conversation about a codebase. Two people: a host who wants to
understand it, and an expert who has read it.

Make it sound like a real recording. People hesitate, repeat themselves, start
over, trail off, laugh, warm up before getting to the point, say "right, right"
while they think. Turns are uneven — sometimes one word, sometimes a long
stretch where the expert just runs with it. None of that reads well on a page
and all of it is what makes audio sound like people rather than an announcement.

Watch the host in particular. If every turn is built the same way — the same
opener, the same rhythm, the same move — it stops sounding like a person and
starts sounding like a form being filled in, however casual the words are.

Two things are not yours to invent:

Facts about the code come from the beats below and nowhere else. If a beat does
not say it, neither does the expert.

Nothing outside this room. No weather, no news, no "I was reading something this
morning" — you cannot check any of it, and a fabricated anecdote is a
fabrication however charming it is. Small talk that claims nothing is fine.

Say names the way you would out loud, and use no markdown — this is speech, not
a page.

Where a beat says HEDGE REQUIRED, the expert has to sound unsure out loud:
"probably", "likely", "seems", "may". Doubt a listener cannot hear is not doubt.

Return JSON: {"turns": [{"order": <beat order>, "host": "...", "expert": "..."}]}
Every beat needs its expert turn. "host" is optional — leave it out when the
expert simply keeps going.
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
            # Not split. A host turn is short by nature — "Wait, hang on. Not
            # even a little?" is three sentences and one breath, and cutting it
            # puts a scene boundary inside a single thought. The cap exists for
            # the expert's long stretches; applying it here buys nothing and
            # costs the delivery.
            segments.append(
                _segment(question, beat, id=f"n{beat.order:03d}q", role="question",
                         speaker=HOST)
            )
        segments.extend(
            _split(answer, beat, prefix=f"n{beat.order:03d}", role="beat", speaker=EXPERT)
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


def _split(text: str, beat, *, prefix: str, role: str, speaker: str) -> list[Segment]:
    """One turn, cut into scene-sized segments.

    A segment is the TTS cache unit and, once the video renderer exists, the
    scene unit — which is why `MAX_SENTENCES` exists at all. But a long stretch
    where the expert simply runs with it is one of the things that makes audio
    sound like a person, so rejecting the turn would be enforcing a video
    constraint on the writing.

    Splitting satisfies both. The words are the model's; the segmentation is
    ours. It also keeps the cache honest — editing the tail of a long answer
    re-synthesizes the tail rather than the whole turn.
    """
    sentences = split_sentences(text)
    if len(sentences) <= MAX_SENTENCES:
        return [_segment(text, beat, id=prefix, role=role, speaker=speaker)]
    chunks = [
        " ".join(sentences[i : i + MAX_SENTENCES])
        for i in range(0, len(sentences), MAX_SENTENCES)
    ]
    return [
        _segment(chunk, beat, id=f"{prefix}-{i}", role=role, speaker=speaker)
        for i, chunk in enumerate(chunks)
    ]


def _segment(text: str, beat, *, id: str, role: str, speaker: str) -> Segment:
    return Segment(
        id=id,
        section_id=beat.section_id,
        beat_order=beat.order,
        text=text,
        confidence=beat.confidence,
        kind=beat.kind,
        role=role,
        speaker=speaker,
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
    """The host's question, and nothing answering it.

    An earlier version had the expert reply with a disclaimer — "everything here
    comes from reading the code at one commit". The first real run showed what
    that does: the model, having been given the same question as beat zero, asks
    it again, and the script opens by asking one question twice with a non-answer
    in between. The judge caught it before I did.

    So the opening is the question alone. The expert's first words are the
    model's, answering it for real, and the caveats that used to live here have
    moved to the end where they were always going to be repeated anyway. This
    also removes the last computed *claim* from the script: a computed line
    cannot be checked against a section, so a question — which asserts nothing —
    is the only thing that belongs here.
    """
    opener = plan.question.strip() or f"What is {plan.title}?"
    if not opener.endswith("?"):
        opener += "?"
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
        )
    ]


def _closing(document: Document | None) -> list[Segment]:
    """The caveats, asked for and then given.

    The host's prompt is computed and fixed. On the page these sit in a box the
    reader sees before starting; in audio, saying nothing is a claim that the
    run saw everything.
    """
    if document is None:
        return []
    warnings = list(document.capabilities.spoken_warnings())
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
