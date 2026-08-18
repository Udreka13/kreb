"""Who speaks which line.

A monologue needs one voice and a dialogue needs two, and the renderer should
not be the thing that knows this. `Cast` is the indirection: `build_audio` asks
it for the engine that speaks a given segment and is otherwise unchanged.

The important property is that a `Cast` is itself a `SpeechEngine`. A single
voice is a cast of one, so there is no second code path through synthesis — the
same loop, the same cache, the same measurement, whether one person is talking
or two. Its `speak` is the default voice's, so anything that treats a cast as a
plain engine gets the narrator rather than an error.

`identity` names every voice in the cast, in a stable order. That is what makes
recasting invalidate the cache: swapping which voice plays the host must not
serve back the old host's audio, and the per-segment key is built from the
speaking engine's identity, so it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kreb.tts.base import Availability, SpeechEngine, Spoken

NARRATOR = "narrator"


@dataclass
class Cast:
    """A default voice, plus a voice per named speaker."""

    default: SpeechEngine
    voices: dict[str, SpeechEngine] = field(default_factory=dict)

    def for_speaker(self, speaker: str) -> SpeechEngine:
        """The voice for this speaker, falling back to the default.

        Falling back rather than raising: a narration written by the dialogue
        renderer and played through a one-voice cast should be audible in one
        voice, not a run of failed segments. The wrong-sounding result is
        obvious on the first listen; a failure here would not be.
        """
        return self.voices.get(speaker, self.default)

    @property
    def identity(self) -> str:
        if not self.voices:
            return self.default.identity
        parts = [f"{name}={self.voices[name].identity}" for name in sorted(self.voices)]
        return f"cast({self.default.identity}; {'; '.join(parts)})"

    @property
    def sample_rate(self) -> int:
        """One rate for the whole cast, taken from the default.

        Concatenation copies streams rather than resampling, so a cast whose
        voices disagree on rate produces a file that either fails to join or
        joins at the wrong speed. Every engine in this project takes its rate as
        a parameter, so the caller sets them equal; this reports the one that
        the joined file will actually have.
        """
        return self.default.sample_rate

    def check(self) -> Availability:
        """Every voice must be available, and each missing one is named.

        Failing on the first missing voice would send someone to fix their
        expert voice, rerun, and be told about the host.
        """
        problems = []
        for name, engine in [(NARRATOR, self.default), *sorted(self.voices.items())]:
            state = engine.check()
            if not state:
                problems.append(f"{name}: {state.reason}")
        if problems:
            return Availability(False, "; ".join(problems))
        return Availability(True)

    def speak(self, text: str, out: Path) -> Spoken:
        return self.default.speak(text, out)


def as_cast(engine: SpeechEngine | Cast) -> Cast:
    """Accept either, work with one. A lone engine is a cast of one."""
    return engine if isinstance(engine, Cast) else Cast(default=engine)
