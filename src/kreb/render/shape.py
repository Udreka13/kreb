"""How long the audio runs, and how far in it goes.

One preset chosen by name, and everything downstream reads from it. The reason
this is a module rather than two CLI flags threaded through five call sites is
that *length is decided in `beats`, not in narration*. The first real run made
that concrete: eight sections became eight beats, 784 words, 4.8 minutes. Asking
the narrator to talk for forty minutes over eight beats does not produce forty
minutes — it produces eight padded beats, which is the same script read slower.
Forty minutes is roughly 6,500 words, which is roughly 45 beats. So the target
has to reach all the way back to what gets planned.

Depth and length are one knob here rather than two, deliberately. They are not
independent in practice: there is only so much surface-level material in a
document, so a long shallow script is a short script repeating itself, and a
deep five-minute script is a list of mechanisms with no room to explain any of
them. Three named combinations that each make sense beat six combinations of
which four do not.

`words` is a *target*, never a check. Nothing rejects a script for coming in
under or over — a rule like that would be enforcing a budget on writing, and the
model is better placed than a validator to know where a point ends. The number's
job is to be in the prompt, and to size the beat plan.
"""

from __future__ import annotations

from dataclasses import dataclass

# Measured on a real run: 784 words over 286.32 seconds of Flux TTS output at
# default speed. Not a general figure for English speech — it is what this
# pipeline's voices actually do, which is what makes minute targets meaningful.
WORDS_PER_MINUTE = 164.0


@dataclass(frozen=True)
class Shape:
    """A target length and a register, named as one choice."""

    name: str
    minutes: int
    depth: str
    audience: str
    """Who the expert is talking to. This is what stops the host reading as
    slow — a host who exists to prompt the next section asks flat questions, and
    a host who is a specific person with specific knowledge does not."""

    detail: str
    """What the expert reaches for when it has room: definitions, or mechanism."""

    @property
    def words(self) -> int:
        return int(self.minutes * WORDS_PER_MINUTE)

    @property
    def beats(self) -> int:
        """Beats needed to fill the time.

        Divided by the words a single beat actually produced on the real run —
        784 words over 8 beats — rather than by a guess. A beat is a point plus
        the exchange around it, so this is not "one sentence".
        """
        return max(4, round(self.words / 98))


PRESETS: dict[str, Shape] = {
    "quick": Shape(
        name="quick",
        minutes=5,
        depth="overview",
        audience=(
            "a working engineer who has not seen this codebase and wants to know "
            "whether it is worth their afternoon"
        ),
        detail="what each thing is for, and how the pieces fit together",
    ),
    "standard": Shape(
        name="standard",
        minutes=15,
        depth="mixed",
        audience=(
            "an engineer who would be comfortable reading this code and wants the "
            "shape of it before they do"
        ),
        detail=(
            "how the important pieces actually work, and why they were built that "
            "way where the document says"
        ),
    ),
    "deep": Shape(
        name="deep",
        minutes=40,
        depth="mechanism",
        audience=(
            "someone who will be working in this codebase next week — they know the "
            "language, they know the general problem, they do not know this solution"
        ),
        detail=(
            "the mechanism: what happens in what order, what the tricky cases are, "
            "which decisions were forced and which were chosen. Assume the listener "
            "can follow a data structure described out loud"
        ),
    ),
}

DEFAULT = "standard"


def shape_for(name: str) -> Shape:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown preset {name!r}; pick one of {', '.join(sorted(PRESETS))}"
        ) from None


def brief(shape: Shape) -> str:
    """The lines a script-writing prompt needs. Phrased as a target, not a rule.

    "Roughly" is doing real work here. A hard word count invites a model to pad
    or truncate to hit it, and both are worse than a script that runs four
    minutes long because the material was there.
    """
    return (
        f"Aim for roughly {shape.minutes} minutes of audio — about "
        f"{shape.words:,} words. That is a target, not a limit: if the material "
        f"runs out sooner, stop.\n\n"
        f"You are talking to {shape.audience}. Go into {shape.detail}."
    )
