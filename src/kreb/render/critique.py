"""Judging a script for coherence — and reporting, never gating.

The properties that decide whether narration is worth listening to are all
semantic: does this sound like two people, does the question set up the answer,
does the thing hold together heard once at speed. None of them is checkable by
counting characters, and the earlier attempt to approximate one of them with a
lexical rule — every host turn ends in a question mark — produced precisely the
robotic output it looked like it was preventing.

So a model judges it. The important decision is where the verdict goes.

**It is not wired into the retry loop.** `research/writer.py` sets the rule and
the reason: a rejection reason that is a semantic judgement teaches a model to
dodge the judgement rather than fix the work. Told "this sounds stilted", a model
does not become natural; it becomes something that does not read as stilted to
the judge. Every rejection this pipeline acts on is mechanically true or false,
and a critique is neither.

What a report buys instead is the thing the lexical rule never could: a number
you can watch across runs, attached to quotes you can check yourself. It is
advisory by construction — `kreb audio` prints the score and exits on the audio,
not on the critique.

Two guards on the judge itself, because a judge is a model and inherits every
weakness of one. It sees the script and nothing else — no beats, no confidence
tags, no document — so it cannot mark a script well for agreeing with its source;
that is Gate A's job and it is done elsewhere with anchors. And every finding
must quote the line it is about, verbatim, or it is dropped: a judge that cannot
point at the text is a judge that is generating plausible criticism.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from kreb.budget.ledger import Charge
from kreb.progress import Progress
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Message, Request
from kreb.render.narration import Narration

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

# What gets judged. Each is a question about the script as heard, and each is
# one a listener could answer — which is the test for whether it belongs here.
AXES: tuple[tuple[str, str], ...] = (
    ("natural", "Does this sound like a real recording of two people?"),
    ("coherent", "Does each turn follow from the one before it?"),
    ("listenable", "Would you keep listening, heard once, without the page?"),
)

# Below this, the script is worth rewriting. Chosen as "clearly mediocre" rather
# than tuned — nothing has enough runs behind it to tune against yet, and a
# threshold presented as calibrated when it is not is worse than an arbitrary
# one that says so.
GOOD_ENOUGH = 3.0


class Finding(BaseModel):
    """One concrete problem, anchored to the line it is about."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quote: str = ""
    problem: str = ""


class CritiqueDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scores: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""


@dataclass
class Critique:
    """What a judge thought, with the receipts."""

    scores: dict[str, int] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    summary: str = ""
    model: str = ""
    cost: float = 0.0
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether a verdict was reached at all. Not whether it was a good one."""
        return bool(self.scores)

    @property
    def score(self) -> float:
        """The mean across axes, or 0.0 if there is no verdict.

        A mean rather than a minimum: one weak axis on an otherwise good script
        is a note, and a minimum would make every script the score of its worst
        moment. The per-axis numbers are kept so nothing is hidden by averaging.
        """
        return round(sum(self.scores.values()) / len(self.scores), 2) if self.scores else 0.0

    @property
    def complete(self) -> bool:
        """Whether every axis was actually scored.

        A judge that skips an axis is not a judge that found it unremarkable.
        The first real run returned two of three and the mean of those two
        landed exactly on the threshold, so a script the same judge called "too
        clean" reported as good enough. Averaging over whatever came back hides
        the gap; naming it does not.
        """
        return {name for name, _ in AXES} <= set(self.scores)

    @property
    def good_enough(self) -> bool:
        """A passing score on a complete verdict. Both halves are load-bearing."""
        return self.ok and self.complete and self.score >= GOOD_ENOUGH


CRITIQUE_SYSTEM = """\
You are judging a scripted conversation about a codebase. It will be heard
aloud, once, by someone who cannot scroll back.

Score each of these 1 to 5, where 3 is "fine, unremarkable" and 5 is "I would
not have guessed this was scripted":

- natural: does this sound like a real recording of two people?
- coherent: does each turn follow from the one before it?
- listenable: would you keep listening, heard once, without the page?

Judge it as audio, not as prose. Hesitation, repetition, half-finished
sentences, someone saying "right, right" while they think — those read badly and
sound human, so do not mark them down. What earns a low score is the opposite:
turns that alternate like clockwork, questions that are interchangeable,
everyone speaking in finished paragraphs. Reserve 5 for something you would not
skip.

Then list what is actually wrong. Every finding must quote the offending line
exactly as it appears — the quote is checked against the script, and a finding
whose quote does not appear is discarded. Say what is wrong with that specific
line, not what the script should be like in general.

You are judging how it *reads and sounds*. You cannot see the codebase, so do
not guess at whether a claim is true; that is checked elsewhere.

Return JSON: {"scores": {"natural": n, "coherent": n, "setup": n,
"substance": n}, "findings": [{"quote": "...", "problem": "..."}],
"summary": "one sentence"}
"""


def critique_user_prompt(narration: Narration) -> str:
    """The script, with speaker labels, and nothing else.

    Deliberately no beats, no confidence tags, no document. A judge that could
    see the source could reward a script for being faithful to it, which is
    Gate A's job and is done with anchors rather than opinions.
    """
    lines = []
    for segment in narration.segments:
        who = segment.speaker.upper() if segment.speaker != "narrator" else "NARRATOR"
        lines.append(f"{who}: {segment.text}")
    return "\n\n".join(lines)


def critique(
    narration: Narration,
    provider: MeteredProvider,
    *,
    role: str = "narrate",
    progress: Progress | None = None,
) -> Critique:
    """Ask a model what it thinks of the script. One attempt, never retried.

    One attempt because there is nothing to retry *toward*: a critique is not
    checkable, so a second opinion is a second opinion rather than a correction,
    and paying for it would be paying for the appearance of rigour.

    A failure here is not a failed run. The audio is unaffected by what a judge
    thinks of it, so every error path returns a `Critique` carrying its reason.
    """
    spent_before = provider.ledger.total(phase=provider.phase)
    script = critique_user_prompt(narration)

    try:
        provider.budget.guard(provider.ledger, phase=provider.phase)
    except Exception as exc:
        return Critique(reason=f"not judged: {exc}")

    completion = None
    try:
        completion = provider.inner.complete(
            Request(
                messages=(Message("system", CRITIQUE_SYSTEM), Message("user", script)),
                role=role,  # type: ignore[arg-type]
                unit="critique",
                response_format={"type": "json_object"},
            )
        )
    except Exception as exc:
        return Critique(reason=f"not judged: {exc}")
    finally:
        if completion is not None:
            provider.ledger.charge(
                Charge.from_usage(
                    phase=provider.phase,
                    unit="critique",
                    role=role,
                    model=completion.model,
                    usage=completion.usage,
                    attempt=1,
                    failed=False,
                )
            )

    spent = provider.ledger.total(phase=provider.phase) - spent_before
    result = _parse(completion.text, script)
    result.model = completion.model
    result.cost = spent

    if progress is not None:
        progress.emit(
            "critique",
            id="critique",
            score=result.score,
            findings=len(result.findings),
            cost=spent,
            reason=result.reason,
        )
    return result


def _parse(text: str, script: str) -> Critique:
    try:
        draft = CritiqueDraft.model_validate_json(_FENCE.sub("", text).strip())
    except Exception as exc:
        return Critique(reason=f"the judge's answer could not be read: {exc}")

    names = {name for name, _ in AXES}
    scores = {
        name: max(1, min(5, int(value)))
        for name, value in draft.scores.items()
        if name in names and isinstance(value, int)
    }
    if not scores:
        return Critique(reason="the judge returned no scores")

    # A finding that cannot point at the script is a finding about a script that
    # was not read. Dropped rather than reported, because a plausible-sounding
    # critique of a line nobody wrote is worse than one fewer note.
    kept, invented = [], 0
    for finding in draft.findings:
        if finding.quote and finding.quote in script:
            kept.append(finding)
        else:
            invented += 1

    notes = []
    missing = sorted({name for name, _ in AXES} - set(scores))
    if missing:
        notes.append(f"the judge did not score {', '.join(missing)}")
    reason = ""
    if invented:
        notes.append(
            f"{invented} finding{'s' if invented > 1 else ''} quoted lines that are "
            "not in the script and were dropped"
        )
    return Critique(
        scores=scores, findings=tuple(kept), summary=draft.summary,
        reason="; ".join(notes),
    )


def revision_note(result: Critique) -> str:
    """The critique as an instruction to write it again, or "" if there is none.

    Only the *quoted* findings go in, and that restriction is the whole reason
    this is allowed to exist at all. `research/writer.py` sets the rule that no
    rejection reason is ever a semantic judgement, because a model told "this
    reads like documentation" stops sounding like documentation rather than
    getting better. A quoted finding is a step away from that: it points at a
    line that is verifiably in the script and says what is wrong with that line.
    Closer to "this symbol does not resolve" than to "this reads badly".

    The summary and the scores are deliberately left out. A model told it scored
    2 out of 5 on "natural" has been handed a target with no text attached, and
    the cheapest way to move that number is to write something blander that no
    judge objects to. Lines it can point at are harder to game.

    It is still not proof against laundering, which is why the revision pass is
    opt-in and both drafts are kept: a revision that made the script worse has to
    be visible, not silently blessed.
    """
    if not result.findings:
        return ""
    notes = "\n".join(
        f'- "{f.quote}" — {f.problem}' for f in result.findings
    )
    return (
        "\n\nYou have written this once already. A listener made these notes on "
        "specific lines:\n"
        f"{notes}\n"
        "Write it again, fixing those lines. Keep what worked — this is a "
        "revision, not a fresh start, and a safer script is not a better one."
    )


def to_json(result: Critique) -> str:
    return json.dumps(
        {
            "score": result.score,
            "good_enough": result.good_enough,
            "complete": result.complete,
            "threshold": GOOD_ENOUGH,
            "scores": result.scores,
            "model": result.model,
            "cost": round(result.cost, 6),
            "summary": result.summary,
            "reason": result.reason,
            "findings": [{"quote": f.quote, "problem": f.problem} for f in result.findings],
        },
        indent=2,
    )


def render(result: Critique) -> str:
    """A few lines for a terminal, not a report."""
    if not result.ok:
        return f"script not judged: {result.reason}"
    axes = "  ".join(f"{name} {result.scores.get(name, '-')}" for name, _ in AXES)
    partial = "" if result.complete else "  [partial verdict]"
    lines = [f"script {result.score}/5  ({axes}){partial}"]
    if result.summary:
        lines.append(f"  {result.summary}")
    for finding in result.findings[:3]:
        lines.append(f'  "{finding.quote[:60]}" — {finding.problem}')
    return "\n".join(lines)
