"""Turning narration into one audio file, and into the timings the video needs.

Two artifacts come out of here and only one of them is audio. The other is
`timings` — where each segment starts and how long it lasts — and it is the
input the video mux will run on. That is why duration is measured with `ffprobe`
rather than estimated from word count: in the mux, `scene_len = max(audio_len,
min_duration)`, so a duration that is wrong by a second is a scene that outlives
its sentence. Duration is a *computed* field everywhere downstream, structurally
impossible to author, and this is the module that computes it.

Synthesis is cached per segment, keyed on the text *and* the engine's identity.
Both halves matter. Text alone means upgrading piper leaves you with a file
where one edited paragraph is in the new timbre and the rest is in the old —
one audible seam, mid-document, that no artifact hash catches. Engine alone
would defeat the point of caching entirely.

When no speech engine is available the run does not fail. It produces the
narration, the timings marked `estimated`, and a stated reason — the same
partial-that-says-so contract the research loop uses when it hits a ceiling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from kreb.media import check_tools, duration_of, run_ffmpeg
from kreb.progress import Progress
from kreb.render.narration import Narration, Segment
from kreb.tts.base import SpeechEngine
from kreb.tts.cast import Cast, as_cast


@dataclass(frozen=True)
class SegmentTiming:
    """Where one segment sits on the timeline."""

    id: str
    section_id: str
    start: float
    seconds: float
    estimated: bool
    text: str
    generation_id: str = ""
    """A hosted engine's receipt for this segment, if there is one.

    Empty for local engines, and empty for a segment served from cache — the
    receipt belongs to the request that made the file, and a cached segment made
    no request. So the trail is partial by construction, which is the honest
    shape: it records what this run was actually billed for, not what the whole
    document would cost.
    """

    @property
    def end(self) -> float:
        return self.start + self.seconds


@dataclass
class AudioResult:
    """The audio if there is any, the timeline either way."""

    timings: tuple[SegmentTiming, ...] = ()
    path: Path | None = None
    engine: str = ""
    reason: str = ""
    synthesized: int = 0
    reused: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """There is an audio file. Not the same as it being the whole document."""
        return self.path is not None

    @property
    def complete(self) -> bool:
        """Every segment made it in.

        The distinction is load-bearing. `beats` enforces that every section
        gets a beat, and a segment silently dropped at synthesis time undoes
        that rule one layer down: you get a file that plays cleanly and is
        missing a section, which is the exact failure the coverage rule exists
        to prevent. Callers exit on this, not on `ok`.
        """
        return self.ok and not self.failures

    @property
    def seconds(self) -> float:
        return self.timings[-1].end if self.timings else 0.0

    @property
    def estimated(self) -> bool:
        """True if any segment's duration was not actually measured."""
        return any(t.estimated for t in self.timings)


def segment_key(segment: Segment, engine: SpeechEngine) -> str:
    """The cache key for one synthesized segment.

    Includes the engine identity — see the module docstring for the seam this
    prevents. Includes the segment id so two identical lines in different places
    stay independently invalidatable, which costs one extra synthesis and buys a
    cache that can be reasoned about.
    """
    digest = hashlib.sha256()
    digest.update(engine.identity.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(segment.id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(segment.text.encode("utf-8"))
    return digest.hexdigest()[:24]


def build_audio(
    narration: Narration,
    engine: SpeechEngine | Cast,
    *,
    out: Path,
    cache_dir: Path,
    progress: Progress | None = None,
) -> AudioResult:
    """Synthesize every segment, join them, and measure the result."""
    cast = as_cast(engine)
    state = cast.check()
    if not state:
        return AudioResult(
            timings=_estimated_timeline(narration, cast.default),
            engine=cast.identity,
            reason=state.reason,
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    result = AudioResult(engine=cast.identity)
    pieces: list[Path] = []
    timings: list[SegmentTiming] = []
    cursor = 0.0

    total = len(narration.segments)
    for position, segment in enumerate(narration.segments, start=1):
        voice = cast.for_speaker(segment.speaker)
        path = cache_dir / f"{segment_key(segment, voice)}.wav"
        cached = path.exists() and path.stat().st_size > 0
        spoken_seconds: float | None = None
        generation_id = ""
        if cached:
            result.reused += 1
        else:
            spoken = voice.speak(segment.text, path)
            if not spoken.ok:
                result.failures.append(f"{segment.id}: {spoken.reason}")
                if progress is not None:
                    progress.emit(
                        "segment",
                        segment.id,
                        done=position,
                        total=total,
                        failed=True,
                        reason=spoken.reason,
                    )
                continue
            result.synthesized += 1
            spoken_seconds = spoken.seconds or None
            generation_id = spoken.generation_id

        measured = duration_of(path)
        seconds = measured if measured is not None else (spoken_seconds or 0.0)
        timings.append(
            SegmentTiming(
                id=segment.id,
                section_id=segment.section_id,
                start=round(cursor, 3),
                seconds=round(seconds, 3),
                estimated=measured is None,
                text=segment.text,
                generation_id=generation_id,
            )
        )
        cursor += seconds
        pieces.append(path)

        if progress is not None:
            progress.emit(
                "segment",
                segment.id,
                done=position,
                total=total,
                seconds=round(seconds, 3),
                cached=cached,
            )

    result.timings = tuple(timings)

    if not pieces:
        result.reason = "no segment could be synthesized"
        return result

    ok, detail = _concat(pieces, out)
    if not ok:
        result.reason = detail
        return result
    result.path = out
    if result.failures:
        # Say it here rather than leaving it to the caller: a partial file that
        # plays cleanly gives a listener no signal that anything is missing.
        result.reason = (
            f"{len(result.failures)} of {len(narration.segments)} segments "
            "could not be synthesized and are missing from the audio"
        )
    return result


def _concat(pieces: list[Path], out: Path) -> tuple[bool, str]:
    """Join wavs with the concat demuxer.

    The demuxer rather than the filter, because it copies the stream instead of
    re-encoding: the segments are already at one sample rate from one engine, so
    a re-encode would spend time to change nothing. The list file is written
    next to the output and removed, and paths are quoted with the demuxer's own
    escaping rule.
    """
    tools = check_tools(need_probe=False)
    if not tools:
        return False, tools.reason

    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.parent / f".{out.name}.concat.txt"
    listing.write_text(
        "".join(f"file '{_quote(p)}'\n" for p in pieces),
        encoding="utf-8",
    )
    try:
        return run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]
        )
    finally:
        listing.unlink(missing_ok=True)


def _quote(path: Path) -> str:
    """Escape a path for the concat demuxer's `file '...'` directive.

    The demuxer's own rule: end the quoted run, emit an escaped quote, reopen.
    Cache paths are hex digests today, so no path this project generates needs
    it — but the cache directory is user-supplied, and a repository checked out
    under `/home/o'brien/` is not an exotic hypothetical.
    """
    return str(path.resolve()).replace("'", "'\\''")


def _estimated_timeline(narration: Narration, engine: SpeechEngine) -> tuple[SegmentTiming, ...]:
    """A timeline built without synthesizing anything.

    Every entry is marked `estimated`, which is the point: the video renderer
    can lay out scenes against it, but nothing can mistake it for a measurement.
    """
    per_second = getattr(engine, "words_per_minute", 150.0) / 60.0
    timings: list[SegmentTiming] = []
    cursor = 0.0
    for segment in narration.segments:
        seconds = max(0.6, len(segment.text.split()) / per_second)
        timings.append(
            SegmentTiming(
                id=segment.id,
                section_id=segment.section_id,
                start=round(cursor, 3),
                seconds=round(seconds, 3),
                estimated=True,
                text=segment.text,
            )
        )
        cursor += seconds
    return tuple(timings)


def timings_json(result: AudioResult) -> str:
    return json.dumps(
        {
            "engine": result.engine,
            "audio": str(result.path) if result.path else None,
            "seconds": round(result.seconds, 3),
            "estimated": result.estimated,
            "complete": result.complete,
            "failures": list(result.failures),
            "reason": result.reason,
            "segments": [
                {
                    "id": t.id,
                    "section_id": t.section_id,
                    "start": t.start,
                    "seconds": t.seconds,
                    "end": round(t.end, 3),
                    "estimated": t.estimated,
                    "generation_id": t.generation_id,
                    "text": t.text,
                }
                for t in result.timings
            ],
        },
        indent=2,
    )
