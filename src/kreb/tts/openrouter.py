"""Hosted speech, through the OpenRouter key this project already has.

The third adapter behind `SpeechEngine`, and the reason the port exists. Nothing
in `render/audio.py` changes to gain a hosted voice: it asks an engine for its
`identity`, calls `speak`, and measures the file that comes back.

Two things about this endpoint shape the code more than anything else.

`response_format` defaults to `"pcm"`, and PCM here is headerless — no sample
rate, no channel count, nothing `ffprobe` can read. A pipeline whose central
promise is *durations are measured, never estimated* cannot accept a stream that
cannot be measured. So this asks for `mp3` and re-encodes to wav at a declared
rate. The re-encode is not waste: it is what makes every engine hand `_concat`
the same container at the same rate, and it makes the model's own output rate —
which is undocumented and provider-chosen — stop mattering.

`identity` names the model, the voice, the speed and the sample rate, and never
the API key. It goes into cache keys and cache keys go into filenames. There is
also a blind spot it cannot close, and naming it is better than pretending
otherwise: the alias `deepgram/flux-tts:free` resolves to a dated build
(`deepgram/flux-tts-20260812:free` at the time of writing), so the host can
change the voice under a stable alias without anything here noticing. A local
piper voice is hashed and cannot do that. It is the standing cost of a hosted
voice, and the reason `PiperEngine` is kept rather than deleted.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kreb.config.secrets import MissingCredential, redact, resolve_api_key
from kreb.media import check_tools, run_ffmpeg
from kreb.tts.base import Availability, Spoken

API_URL = "https://openrouter.ai/api/v1/audio/speech"
SPEECH_TIMEOUT = 120.0

# Free at the time of writing — prompt and completion both priced "0" — and the
# reason a hosted voice is the default at all. A default that costs money per
# paragraph would make `kreb audio` something you think twice about running.
DEFAULT_MODEL = "deepgram/flux-tts:free"

# Not the model's native rate, which is provider-chosen and undocumented. This
# is the rate everything is re-encoded *to*, so that one concat can copy streams
# instead of resampling. 24kHz is the common neural-TTS output rate; picking the
# same number avoids an upsample that adds nothing.
DEFAULT_SAMPLE_RATE = 24000

# What the request asks for. Not configurable: see the module docstring — `pcm`
# is unmeasurable, and offering a choice would offer a way to break timings.
WIRE_FORMAT = "mp3"

# (bytes, generation-id). Injected in tests so the wav-conversion path runs for
# real against real mp3 bytes without a key or a network.
Transport = Callable[[dict], tuple[bytes, str]]


@dataclass
class OpenRouterVoice:
    """Speech from a hosted model, re-encoded to this project's wav contract."""

    model: str = DEFAULT_MODEL
    voice: str = ""
    speed: float = 1.0
    sample_rate: int = DEFAULT_SAMPLE_RATE
    timeout: float = SPEECH_TIMEOUT
    referer: str = "https://github.com/Udreka13/kreb"
    transport: Transport | None = field(default=None, repr=False)

    # The key is deliberately not a field. As a field it lands in every
    # traceback and every pytest failure line that renders this object — the
    # same leak `OpenRouterProvider.__repr__` exists to prevent, arrived at from
    # the other direction. It is resolved at the moment of use instead.

    def check(self) -> Availability:
        """Both halves, both named — a key without ffmpeg fails just as hard."""
        missing = []
        if resolve_api_key(required=False) is None:
            missing.append("no OpenRouter API key (set OPENROUTER_API_KEY)")
        tools = check_tools(need_probe=False)
        if not tools:
            missing.append("ffmpeg is not on PATH (needed to decode the reply)")
        if missing:
            return Availability(False, "; ".join(missing))
        return Availability(True)

    @property
    def identity(self) -> str:
        """`openrouter/<model>/<voice>/sp<speed>/<rate>`.

        The rate belongs here because this engine re-encodes to it: change the
        rate and the same words at the same speed in the same voice produce
        different bytes. Leaving it out would serve the old rate from cache.
        """
        return "/".join(
            [
                "openrouter",
                self.model,
                self.voice or "default",
                f"sp{self.speed:g}",
                str(self.sample_rate),
            ]
        )

    def speak(self, text: str, out: Path) -> Spoken:
        state = self.check()
        if not state:
            return Spoken(path=None, reason=state.reason)

        payload: dict = {"model": self.model, "input": text, "response_format": WIRE_FORMAT}
        # Omitted rather than sent empty: valid voice names are per-model, and a
        # blank one is a rejected request where no voice at all is a default.
        if self.voice:
            payload["voice"] = self.voice
        if self.speed != 1.0:
            payload["speed"] = self.speed

        try:
            audio, generation_id = (self.transport or self._post)(payload)
        except SpeechFailed as exc:
            return Spoken(path=None, reason=str(exc))

        if not audio:
            return Spoken(path=None, reason=f"{self.model} returned no audio")

        out.parent.mkdir(parents=True, exist_ok=True)
        encoded = out.parent / f".{out.name}.{WIRE_FORMAT}"
        encoded.write_bytes(audio)
        try:
            ok, detail = run_ffmpeg(
                ["-i", str(encoded), "-ar", str(self.sample_rate), "-ac", "1", str(out)]
            )
        finally:
            encoded.unlink(missing_ok=True)

        if not ok:
            return Spoken(path=None, reason=f"could not decode the reply from {self.model}: {detail}")
        if not out.exists() or out.stat().st_size == 0:
            return Spoken(path=None, reason=f"{self.model} produced an empty file")
        return Spoken(path=out, generation_id=generation_id)

    def _post(self, payload: dict) -> tuple[bytes, str]:
        key = resolve_api_key()
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": self.referer,
                "X-Title": "kreb",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                kind = response.headers.get("Content-Type", "")
                generation_id = response.headers.get("X-Generation-Id", "")
        except urllib.error.HTTPError as exc:
            raise SpeechFailed(_http_reason(exc, key)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechFailed(f"could not reach OpenRouter: {redact(str(exc), key)}") from exc

        # A 200 carrying JSON is an error the transport did not flag. Reporting
        # it as "returned no audio" would send someone looking at their text.
        if "json" in kind.lower():
            raise SpeechFailed(f"{self.model} answered with an error: {_detail(body)}")
        return body, generation_id


class SpeechFailed(Exception):
    """A synthesis request that did not come back with audio."""


def _http_reason(exc: urllib.error.HTTPError, key: str | None) -> str:
    """The status, and what the body actually said.

    The body matters most on 429: a free-tier cap says which limit was hit and
    when it resets, and that sentence is the whole difference between "rerun it
    tomorrow" and a bug hunt. Nothing is retried here — `build_audio` caches per
    segment, so a capped run resumes at the segment it stopped on.
    """
    try:
        detail = _detail(exc.read())
    except Exception:  # noqa: BLE001 - a body that cannot be read is not the error
        detail = ""
    label = "rate limited by OpenRouter" if exc.code == 429 else f"OpenRouter returned {exc.code}"
    return redact(f"{label}: {detail}" if detail else label, key)


def _detail(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", error))[:200]
    return str(error or text)[:200]


def resolvable() -> bool:
    """Whether a key is available at all, without raising."""
    try:
        return resolve_api_key(required=False) is not None
    except MissingCredential:  # pragma: no cover - required=False cannot raise
        return False
