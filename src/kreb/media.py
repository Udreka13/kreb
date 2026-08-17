"""The ffmpeg boundary, in one place.

Both `tts/` and `render/audio.py` shell out to ffmpeg, and the video renderer
will too. Keeping the invocations here means the missing-binary message is
written once and the `-nostdin` flag — which is what stops a stray ffmpeg from
swallowing the terminal during a long run — cannot be forgotten in one call site
out of six.

Durations are read with `ffprobe` and never estimated. A duration guessed from
word count is wrong by seconds on a paragraph, and in the video mux a duration
that is wrong is a caption that outlives its scene.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG_TIMEOUT = 300.0
PROBE_TIMEOUT = 30.0


@dataclass(frozen=True)
class MediaTools:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def check_tools(*, need_probe: bool = True) -> MediaTools:
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if need_probe and shutil.which("ffprobe") is None:
        missing.append("ffprobe")
    if missing:
        return MediaTools(
            False, f"{' and '.join(missing)} not on PATH; install ffmpeg to build audio"
        )
    return MediaTools(True)


def run_ffmpeg(args: list[str], *, timeout: float = FFMPEG_TIMEOUT) -> tuple[bool, str]:
    """Run ffmpeg. Returns (ok, detail) — detail is the tail of stderr on failure."""
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg is not on PATH"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ffmpeg failed: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", "replace").strip()[-300:]
    return True, ""


def duration_of(path: Path | str) -> float | None:
    """Seconds of media at `path`, or None if it cannot be read.

    None rather than 0.0 on failure: a zero-length segment is a legitimate,
    if odd, result, and collapsing "silent" into "unmeasurable" would let a
    broken probe pass as a valid timing artifact.
    """
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
