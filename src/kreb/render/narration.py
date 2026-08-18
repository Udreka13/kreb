"""Narration for audio — self-contained spoken prose, one segment per beat.

A listener cannot scroll back, cannot see a `speculative` tag, and cannot skim
the caveats box at the top of the page. Every affordance the HTML renderer gets
for free has to be *said* here or it does not exist. That is the whole design
constraint, and three rules fall out of it.

**Uncertainty must be audible.** A section marked `speculative` becomes a
sentence spoken in the same confident voice as everything else unless something
forces a hedge into the words. So: every segment whose beat requires hedging
must contain one of `HEDGES`. This is a *positive lexical requirement* and that
is exactly why it is enforceable — the pipeline's standing rule is that positive
requirements can be checked and negative semantic ones ("does not sound
overconfident") cannot.

**The background signpost is computed, not requested.** Where hedging has to
come from the model — you cannot mechanically insert "probably" into a sentence
and get English — a signpost can simply be prepended. So it is. Asking for it
would make it omittable; prepending makes it structural, the same move as
duration being a computed field in the video mux.

**The caveats are spoken.** A run that could not read git history, or that ran
against a dirty tree, says so out loud at the end. On the page that is a box the
reader can see; in audio, silence about it is a claim of completeness.

Segments are one beat each and capped at `MAX_SENTENCES`, because the segment is
simultaneously the TTS cache unit and — once the video renderer exists — the
scene unit. A four-sentence segment is a scene that sits on screen too long and
a cache entry that reruns on every edit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from kreb.budget.ledger import Charge
from kreb.doc.schema import Confidence, Document, SectionKind
from kreb.index.repo_index import RepoIndex
from kreb.progress import Progress
from kreb.prose import split_sentences
from kreb.provider.metered import MeteredProvider
from kreb.provider.types import Message, Request
from kreb.render.beats import BeatsPlan, unlicensed_symbols

MAX_SENTENCES = 2

# Prepended to every background beat, never asked for. Phrased to survive being
# heard once at speed, without a visual to lean on.
BACKGROUND_PREFIX = "Stepping outside this codebase for a moment:"

# The allowlist a hedged segment must draw from. Kept deliberately plain — these
# are words that survive being heard once, unlike "putatively" or "arguably".
HEDGES: tuple[str, ...] = (
    "probably",
    "likely",
    "appears to",
    "appear to",
    "seems",
    "seem to",
    "suggests",
    "suggest",
    "may",
    "might",
    "could",
    "possibly",
    "apparently",
    "presumably",
    "i think",
    "it looks like",
    "not certain",
    "unconfirmed",
    "unverified",
    "no evidence",
    "without confirming",
    "worth checking",
)

# Word-boundary alternation over the allowlist, longest first so "appears to"
# is preferred over a bare "appear" that is not in the list anyway.
_HEDGE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in sorted(HEDGES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_CODE_SPAN = re.compile(r"`([^`]*)`")
_PAREN = re.compile(r"\s*\([^)]*\)")
_RATIO = re.compile(r"\b(\d+)/(\d+)\b")


@dataclass(frozen=True)
class Segment:
    """One spoken unit: a cache key for TTS, and later a scene."""

    id: str
    section_id: str
    beat_order: int
    text: str
    confidence: Confidence
    kind: SectionKind
    role: str = "beat"
    speaker: str = "narrator"
    """Who says this line. One value for the monologue renderer, two for the
    dialogue one. It is part of the TTS cache key by way of the voice it selects,
    so a segment reassigned to the other host re-synthesizes rather than keeping
    the first host's timbre."""

    @property
    def sentences(self) -> list[str]:
        return split_sentences(self.text)


@dataclass
class Narration:
    """The whole spoken document."""

    title: str
    question: str
    base_sha: str
    segments: tuple[Segment, ...] = ()

    @property
    def script(self) -> str:
        return "\n\n".join(s.text for s in self.segments)

    @property
    def words(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)


@dataclass
class NarrationResult:
    narration: Narration | None
    attempts: int = 0
    rejections: list[str] = field(default_factory=list)
    cost: float = 0.0

    @property
    def ok(self) -> bool:
        return self.narration is not None


class LineDraft(BaseModel):
    """One spoken line. The model writes words and nothing else."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order: int
    text: str


class ScriptDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lines: list[LineDraft] = Field(default_factory=list)


def has_hedge(text: str) -> bool:
    """Whether a segment carries an audible hedge from the allowlist.

    Matched on word boundaries, not as substrings. Substring matching fails in
    both directions and both are reachable: "The mighty parser" contains
    "might" and would pass unhedged, while a line ending "…as it may." would
    fail because the pattern carried a trailing space. A rule this load-bearing
    must not be defeatable by a coincidence of spelling.
    """
    return _HEDGE_RE.search(text) is not None


def speakable(text: str) -> str:
    """Strip the markup a narrator cannot pronounce.

    Backticks are the only markup that reliably survives into narration prose,
    because the source document is full of them and models copy the habit. Read
    aloud, a backtick is nothing; left in, TTS engines pronounce it or stumble.
    """
    return _CODE_SPAN.sub(r"\1", text).replace("**", "").replace("*", "")


NARRATION_SYSTEM = """\
You are writing narration to be spoken aloud about a codebase, one line per beat.

The listener cannot see anything and cannot go back. Write for the ear:

- One line per beat, in the order given. Two sentences at most, one is often
  better. Say the thing; do not introduce it first.
- Self-contained. No "as mentioned above", no "the following", no "this
  section". A line that only makes sense after the previous one is a line that
  breaks when a scene is cut.
- Speak names plainly. Say "the retry policy" rather than spelling out
  `RetryPolicy` character by character, but do not invent a name for something.
- Name only symbols the beat itself names. Adding one is a claim nobody checked.
- No markdown. No backticks, no bullets, no headings. This is speech.
- Where a beat is marked HEDGE REQUIRED, the line must actually sound uncertain
  out loud — use a word like "probably", "likely", "appears to", "may" or
  "seems". Uncertainty a listener cannot hear is not uncertainty.

Return JSON: {"lines": [{"order": <beat order>, "text": "..."}, ...]}
"""


def narration_user_prompt(plan: BeatsPlan) -> str:
    parts = [f"Document: {plan.title}"]
    if plan.question:
        parts.append(f"Question it answers: {plan.question}")
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


def write_narration(
    plan: BeatsPlan,
    index: RepoIndex,
    provider: MeteredProvider,
    *,
    document: Document | None = None,
    role: str = "narrate",
    max_attempts: int = 3,
    progress: Progress | None = None,
) -> NarrationResult:
    """Expand a beat plan into spoken prose."""
    base_user = narration_user_prompt(plan)
    rejections: list[str] = []
    spent_before = provider.ledger.total(phase=provider.phase)

    for attempt in range(1, max_attempts + 1):
        provider.budget.guard(provider.ledger, phase=provider.phase)

        user = base_user + (_retry_suffix(rejections[-4:]) if rejections else "")
        completion = provider.inner.complete(
            Request(
                messages=(Message("system", NARRATION_SYSTEM), Message("user", user)),
                role=role,  # type: ignore[arg-type]
                unit="narration",
                response_format={"type": "json_object"},
            )
        )

        narration, failures = None, []
        try:
            narration, failures = _evaluate(completion.text, plan, index, document)
        except BaseException:
            failures = ["evaluation raised while checking this narration"]
            raise
        finally:
            provider.ledger.charge(
                Charge.from_usage(
                    phase=provider.phase,
                    unit="narration",
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
                id="narration",
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
        draft = ScriptDraft.model_validate_json(_FENCE.sub("", text).strip())
    except Exception as exc:
        return None, [f"output could not be parsed: {exc}"]

    lines = {line.order: line.text for line in draft.lines}
    sections = {s.id: s for s in document.sections} if document is not None else {}
    failures: list[str] = []
    segments: list[Segment] = []

    for beat in plan.beats:
        body = speakable(lines.get(beat.order, "").strip())
        if not body:
            failures.append(f"beat {beat.order} got no line")
            continue

        count = len(split_sentences(body))
        if count > MAX_SENTENCES:
            failures.append(
                f"beat {beat.order} is {count} sentences; at most {MAX_SENTENCES}, "
                "because one segment is one scene"
            )
            continue

        if beat.hedge_required and not has_hedge(body):
            failures.append(
                f"beat {beat.order} comes from a speculative section but the line "
                "states it flatly; say it with one of: "
                + ", ".join(h.strip() for h in HEDGES[:6])
            )
            continue

        section = sections.get(beat.section_id)
        if section is not None:
            stray = unlicensed_symbols(body, section, index)
            if stray:
                failures.append(
                    f"beat {beat.order} names {', '.join(f'`{s}`' for s in sorted(stray))}, "
                    "which its section does not cite; say it without them"
                )
                continue

        # The signpost is prepended rather than requested — see the module
        # docstring. A model asked for it forgets it on beat nineteen.
        if beat.prefix_required:
            body = f"{BACKGROUND_PREFIX} {body[0].lower() + body[1:]}"

        segments.append(
            Segment(
                id=f"n{beat.order:03d}",
                section_id=beat.section_id,
                beat_order=beat.order,
                text=body,
                confidence=beat.confidence,
                kind=beat.kind,
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


def _opening(plan: BeatsPlan) -> list[Segment]:
    """A spoken title card. Computed, because a listener who joins an audio file
    has none of the context a reader gets from a page they chose to open."""
    text = f"{plan.title}."
    if plan.question:
        text += f" This answers the question: {plan.question}"
    return [
        Segment(
            id="n000-open",
            section_id="",
            beat_order=-1,
            text=speakable(text),
            confidence="verified",
            kind="overview",
            role="opening",
        )
    ]


def _closing(document: Document | None) -> list[Segment]:
    """The capabilities caveats, spoken.

    On the page these sit in a box the reader can see before they start. In
    audio there is no equivalent to a box, so not saying them is a claim that
    the run saw everything.
    """
    if document is None:
        return []
    warnings = list(document.capabilities.warnings())
    if not warnings:
        return []
    joined = " ".join(_for_the_ear(w).rstrip(".") + "." for w in warnings)
    return [
        Segment(
            id="n999-close",
            section_id="",
            beat_order=10**6,
            text=speakable(f"One caveat about this run. {joined}"),
            confidence="verified",
            kind="overview",
            role="closing",
        )
    ]


def _for_the_ear(text: str) -> str:
    """Rewrite a caveat written for a page into one that survives being heard.

    The caveats are shared verbatim with the HTML renderer, where a
    parenthetical count like `(14/64)` is a useful glance and a slash is read as
    "out of". Spoken, the parenthesis is inaudible and the slash comes out as
    "fourteen slash sixty-four" or is swallowed entirely. Found by listening to
    the first real run rather than by reading the code.
    """
    text = _PAREN.sub("", text)
    text = _RATIO.sub(r"\1 of \2", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _retry_suffix(reasons: list[str]) -> str:
    return "\n\nThe previous attempt was rejected:\n" + "\n".join(f"- {r}" for r in reasons)


def to_json(narration: Narration) -> str:
    return json.dumps(
        {
            "title": narration.title,
            "question": narration.question,
            "base_sha": narration.base_sha,
            "segments": [
                {
                    "id": s.id,
                    "section_id": s.section_id,
                    "beat_order": s.beat_order,
                    "text": s.text,
                    "confidence": s.confidence,
                    "kind": s.kind,
                    "role": s.role,
                    "speaker": s.speaker,
                }
                for s in narration.segments
            ],
        },
        indent=2,
    )


def from_json(text: str) -> Narration:
    data = json.loads(text)
    return Narration(
        title=data["title"],
        question=data.get("question", ""),
        base_sha=data.get("base_sha", ""),
        segments=tuple(
            Segment(
                id=s["id"],
                section_id=s.get("section_id", ""),
                beat_order=s.get("beat_order", i),
                text=s["text"],
                confidence=s.get("confidence", "derived"),
                kind=s.get("kind", "structure"),
                role=s.get("role", "beat"),
                speaker=s.get("speaker", "narrator"),
            )
            for i, s in enumerate(data.get("segments", ()))
        ),
    )
