"""A voice that says nothing, at the right length.

This is not only a test double. Every part of the audio pipeline downstream of
synthesis — concatenation, probing, the timing artifact the video mux will
consume — depends on durations, not on speech. `SilenceEngine` produces a wav of
the length the words would take at a stated speaking rate, so all of that is
buildable and checkable on a machine with no TTS installed at all, which is the
situation most machines are in.

What it must not do is pretend. It reports `estimated=True` on every timing it
produces, so nothing downstream can quietly present a word-count estimate as a
measurement of speech.

Sample rate and channel count are fixed rather than configurable, because the
concat step joins segments without resampling and a mixed-rate stream is either
a re-encode or a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kreb.media import check_tools, run_ffmpeg
from kreb.tts.base import Availability, Spoken

# A measured, unhurried technical delivery. Piper's default voices land near
# this, which is what makes the estimate useful as a stand-in rather than
# merely well-defined.
WORDS_PER_MINUTE = 150.0

# Every segment gets a little air at the end. Without it, concatenated speech
# runs each sentence into the next with no breath, which is the single most
# recognizable artefact of stitched TTS.
TAIL_SECONDS = 0.35
MIN_SECONDS = 0.6


@dataclass
class SilenceEngine:
    """Silence of the duration the text would take to speak."""

    words_per_minute: float = WORDS_PER_MINUTE
    sample_rate: int = 22050
    tail: float = TAIL_SECONDS
    estimated: bool = True

    @property
    def identity(self) -> str:
        return f"silence/{self.words_per_minute:g}wpm/{self.sample_rate}/{self.tail:g}"

    def check(self) -> Availability:
        tools = check_tools(need_probe=False)
        if not tools:
            return Availability(False, tools.reason)
        return Availability(True)

    def seconds_for(self, text: str) -> float:
        words = len(text.split())
        return max(MIN_SECONDS, words / self.words_per_minute * 60.0 + self.tail)

    def speak(self, text: str, out: Path) -> Spoken:
        state = self.check()
        if not state:
            return Spoken(path=None, reason=state.reason)
        seconds = self.seconds_for(text)
        out.parent.mkdir(parents=True, exist_ok=True)
        ok, detail = run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={self.sample_rate}:cl=mono",
                "-t",
                f"{seconds:.3f}",
                "-c:a",
                "pcm_s16le",
                str(out),
            ]
        )
        if not ok:
            return Spoken(path=None, reason=f"could not write silence: {detail}")
        return Spoken(path=out, seconds=seconds)
