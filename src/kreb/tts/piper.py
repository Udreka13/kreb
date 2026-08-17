"""Piper, driven as a subprocess.

A subprocess and not a Python dependency, deliberately. kreb has three runtime
dependencies and the reason that number is small is that it is defended one
decision at a time; a neural TTS stack would be the largest thing in the tree by
two orders of magnitude, installed for everyone in order to be used by the
people who want narration. So piper is treated exactly like `d2`: optional,
found on `PATH`, and when it is missing the pipeline says so rather than failing
or — worse — silently producing a shorter video.

The voice model is hashed into `identity`, not just named. Voice files get
re-downloaded, replaced, and renamed; two different voices under one filename is
the failure this prevents.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from kreb.tts.base import Availability, Spoken

PIPER_TIMEOUT = 120.0

# Piper's own default for the voices it ships. Declared here rather than probed,
# because probing means synthesizing, and this value is needed before the first
# synthesis in order to build a cache key.
DEFAULT_SAMPLE_RATE = 22050


@dataclass
class PiperEngine:
    """Speech from a local piper binary and a voice model on disk."""

    voice: Path | None = None
    binary: str = "piper"
    length_scale: float = 1.0
    sample_rate: int = DEFAULT_SAMPLE_RATE
    _identity: str = field(default="", init=False, repr=False)

    def check(self) -> Availability:
        """Both halves are checked, and both are named.

        Reporting only "piper is not installed" to someone who installed piper
        and skipped the 60MB voice download sends them to re-check the thing
        that is already fine.
        """
        missing = []
        if shutil.which(self.binary) is None:
            missing.append(f"the `{self.binary}` binary is not on PATH")
        if self.voice is None:
            missing.append("no voice model was configured (`--voice path/to/model.onnx`)")
        elif not Path(self.voice).exists():
            missing.append(f"the voice model {self.voice} does not exist")
        if missing:
            return Availability(False, "; ".join(missing))
        return Availability(True)

    @property
    def identity(self) -> str:
        """`piper/<version>/<voice digest>/<length scale>`.

        Computed once per engine. The voice digest is over the model file's
        bytes, so a re-downloaded or swapped voice under the same filename
        produces a different key and re-synthesizes rather than leaving one
        segment in the old timbre.
        """
        if not self._identity:
            self._identity = "/".join(
                ["piper", self._version(), self._voice_digest(), f"ls{self.length_scale:g}"]
            )
        return self._identity

    def _version(self) -> str:
        if shutil.which(self.binary) is None:
            return "absent"
        try:
            proc = subprocess.run(
                [self.binary, "--version"], capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
        return text.splitlines()[0][:40] if text else "unknown"

    def _voice_digest(self) -> str:
        if self.voice is None or not Path(self.voice).exists():
            return "novoice"
        digest = hashlib.sha256()
        with open(self.voice, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]

    def speak(self, text: str, out: Path) -> Spoken:
        state = self.check()
        if not state:
            return Spoken(path=None, reason=state.reason)

        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [
                    self.binary,
                    "--model",
                    str(self.voice),
                    "--length_scale",
                    str(self.length_scale),
                    "--output_file",
                    str(out),
                ],
                input=text.encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=PIPER_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Spoken(path=None, reason=f"piper failed: {exc}")

        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
            return Spoken(path=None, reason=f"piper rejected the text: {detail}")
        if not out.exists() or out.stat().st_size == 0:
            return Spoken(path=None, reason="piper wrote no audio")
        return Spoken(path=out)
