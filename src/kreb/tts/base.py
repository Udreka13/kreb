"""The speech port: what the audio renderer is allowed to know about a voice.

`tts/` is lifted out of `render/audio/` on purpose. Text-to-speech is the part of
this pipeline most likely to be swapped — piper today, something hosted or local
and better next year — and a renderer that imported a piper subprocess directly
would drag every one of those swaps through itself.

The interesting member is `identity`. It is not decoration: it goes into the
audio cache key. Two runs that produce the same words must produce the same
audio *only if the voice is also the same*. Without it, upgrading piper and
editing one paragraph gives you a file where a single segment is in a different
timbre — one clean seam, mid-sentence, that nothing in the pipeline flags because
every artifact hash says the run was clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Availability:
    """Whether speech can be synthesized, and if not, precisely what is missing."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class Spoken:
    """One synthesized segment, or the reason there isn't one."""

    path: Path | None
    seconds: float = 0.0
    reason: str = ""
    generation_id: str = ""
    """A hosted engine's receipt for this segment, empty for local ones.

    Carried rather than resolved into a cost here. Looking the price up is one
    extra request per segment against an endpoint that lags behind the
    generation, and the recommended voice is free, where the cost is exactly
    zero and needs no lookup. What this buys is that a paid run leaves a trail
    someone can reconcile — the project's rule is that cost is measured, and an
    unreconciled receipt is honest about being unreconciled in a way that a
    guessed number would not be.
    """

    @property
    def ok(self) -> bool:
        return self.path is not None


@runtime_checkable
class SpeechEngine(Protocol):
    """Anything that can turn a line of text into a wav file."""

    @property
    def identity(self) -> str:
        """A stable string naming this engine *and its voice*, for cache keys.

        Must change when the binary version changes, when the voice model
        changes, and when any parameter that affects the waveform changes.
        """
        ...

    @property
    def sample_rate(self) -> int:
        """Fixed per engine. Concatenation of mismatched rates is a resample or
        a failure, and neither belongs in the renderer."""
        ...

    def check(self) -> Availability: ...

    def speak(self, text: str, out: Path) -> Spoken: ...
