"""The hosted voice adapter.

No key and no network here. The transport is injected, but the *bytes* are real
mp3 produced by ffmpeg, so the decode-and-resample path — the part that makes a
hosted reply into something `_concat` can copy and `ffprobe` can measure — runs
for real rather than being mocked past.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import urllib.error
from argparse import Namespace
from pathlib import Path

import pytest

from kreb.cli import engine_for
from kreb.media import duration_of
from kreb.tts.openrouter import (
    DEFAULT_MODEL,
    OpenRouterVoice,
    SpeechFailed,
    _detail,
    _http_reason,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def keyed(monkeypatch):
    """A resolvable key, without touching the environment's real one."""
    monkeypatch.setattr("kreb.tts.openrouter.resolve_api_key", lambda **_: "sk-or-v1-TESTKEY")
    return "sk-or-v1-TESTKEY"


@pytest.fixture(scope="module")
def mp3_bytes(tmp_path_factory) -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("wire") / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
            "-ar", "44100", "-ac", "1", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def transport_of(audio: bytes, generation_id: str = "gen-abc", seen: list | None = None):
    def transport(payload: dict) -> tuple[bytes, str]:
        if seen is not None:
            seen.append(payload)
        return audio, generation_id

    return transport


# -- identity ---------------------------------------------------------------


def test_identity_names_the_model_the_voice_and_the_speed():
    engine = OpenRouterVoice(model="deepgram/flux-tts:free", voice="nova", speed=1.1)
    assert "deepgram/flux-tts:free" in engine.identity
    assert "nova" in engine.identity
    assert "sp1.1" in engine.identity


def test_a_different_voice_is_a_different_identity():
    """Two voices of one model are two timbres. Sharing a cache key across them
    puts both in one file with a seam where the cache happened to be warm."""
    a = OpenRouterVoice(model="m/x", voice="alloy")
    b = OpenRouterVoice(model="m/x", voice="nova")
    assert a.identity != b.identity


def test_a_different_sample_rate_is_a_different_identity():
    """This engine re-encodes *to* its declared rate, so the rate is part of
    what the bytes are — not a detail of how they were fetched."""
    a = OpenRouterVoice(model="m/x", sample_rate=24000)
    b = OpenRouterVoice(model="m/x", sample_rate=16000)
    assert a.identity != b.identity


def test_the_identity_never_carries_the_key(keyed):
    engine = OpenRouterVoice(model="m/x")
    assert keyed not in engine.identity


def test_the_repr_never_carries_the_key(keyed):
    """`repr` lands in tracebacks and pytest output. The key is not a field at
    all, which is the strongest form of this guarantee available."""
    engine = OpenRouterVoice(model="m/x")
    assert keyed not in repr(engine)
    assert "api_key" not in repr(engine)


# -- availability -----------------------------------------------------------


def test_a_missing_key_is_named(monkeypatch):
    monkeypatch.setattr("kreb.tts.openrouter.resolve_api_key", lambda **_: None)
    state = OpenRouterVoice().check()
    assert not state
    assert "API key" in state.reason


def test_a_missing_ffmpeg_is_named_too(keyed, monkeypatch):
    """A key without a decoder fails just as hard, and someone who has just set
    their key should not be sent back to check it."""
    monkeypatch.setattr("kreb.media.shutil.which", lambda name: None)
    state = OpenRouterVoice().check()
    assert not state
    assert "ffmpeg" in state.reason


def test_both_halves_are_named_at_once(monkeypatch):
    monkeypatch.setattr("kreb.tts.openrouter.resolve_api_key", lambda **_: None)
    monkeypatch.setattr("kreb.media.shutil.which", lambda name: None)
    reason = OpenRouterVoice().check().reason
    assert "API key" in reason and "ffmpeg" in reason


def test_an_unavailable_engine_does_not_call_the_transport(monkeypatch, tmp_path):
    monkeypatch.setattr("kreb.tts.openrouter.resolve_api_key", lambda **_: None)
    calls = []
    engine = OpenRouterVoice(transport=transport_of(b"x", seen=calls))
    spoken = engine.speak("hello", tmp_path / "a.wav")
    assert not spoken.ok
    assert calls == []


# -- synthesis --------------------------------------------------------------


@needs_ffmpeg
def test_a_reply_becomes_a_wav_at_the_declared_rate(keyed, mp3_bytes, tmp_path):
    engine = OpenRouterVoice(sample_rate=16000, transport=transport_of(mp3_bytes))
    out = tmp_path / "seg.wav"
    spoken = engine.speak("hello there", out)

    assert spoken.ok
    assert out.exists() and out.stat().st_size > 0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(out)],
        capture_output=True, check=True, text=True,
    )
    assert probe.stdout.strip().startswith("16000,1")


@needs_ffmpeg
def test_the_result_is_measurable(keyed, mp3_bytes, tmp_path):
    """The whole reason `mp3` is requested instead of the endpoint's default
    `pcm`: raw pcm carries no header, so nothing downstream can measure it, and
    every duration in this project is measured."""
    engine = OpenRouterVoice(transport=transport_of(mp3_bytes))
    out = tmp_path / "seg.wav"
    assert engine.speak("hello", out).ok
    seconds = duration_of(out)
    assert seconds is not None and 1.0 < seconds < 2.5


@needs_ffmpeg
def test_the_intermediate_file_is_cleaned_up(keyed, mp3_bytes, tmp_path):
    engine = OpenRouterVoice(transport=transport_of(mp3_bytes))
    out = tmp_path / "seg.wav"
    engine.speak("hello", out)
    assert list(tmp_path.glob("*.mp3")) == []
    assert list(tmp_path.glob(".*")) == []


@needs_ffmpeg
def test_the_generation_id_is_carried_back(keyed, mp3_bytes, tmp_path):
    """A paid run's receipt. Not turned into a cost here — an unreconciled id is
    honest about being unreconciled; a guessed price would not be."""
    engine = OpenRouterVoice(transport=transport_of(mp3_bytes, generation_id="gen-777"))
    spoken = engine.speak("hello", tmp_path / "seg.wav")
    assert spoken.generation_id == "gen-777"


@needs_ffmpeg
def test_the_request_asks_for_mp3(keyed, mp3_bytes, tmp_path):
    seen: list[dict] = []
    engine = OpenRouterVoice(transport=transport_of(mp3_bytes, seen=seen))
    engine.speak("hello", tmp_path / "seg.wav")
    assert seen[0]["response_format"] == "mp3"


@needs_ffmpeg
def test_an_unset_voice_is_omitted_not_sent_empty(keyed, mp3_bytes, tmp_path):
    """Valid voice names are per-model. No voice at all is a model default; an
    empty string is a rejected request."""
    seen: list[dict] = []
    engine = OpenRouterVoice(voice="", transport=transport_of(mp3_bytes, seen=seen))
    engine.speak("hello", tmp_path / "seg.wav")
    assert "voice" not in seen[0]


@needs_ffmpeg
def test_a_set_voice_is_sent(keyed, mp3_bytes, tmp_path):
    seen: list[dict] = []
    engine = OpenRouterVoice(voice="nova", transport=transport_of(mp3_bytes, seen=seen))
    engine.speak("hello", tmp_path / "seg.wav")
    assert seen[0]["voice"] == "nova"


@needs_ffmpeg
def test_a_default_speed_is_omitted(keyed, mp3_bytes, tmp_path):
    seen: list[dict] = []
    engine = OpenRouterVoice(speed=1.0, transport=transport_of(mp3_bytes, seen=seen))
    engine.speak("hello", tmp_path / "seg.wav")
    assert "speed" not in seen[0]


def test_an_empty_reply_is_a_stated_failure(keyed, tmp_path):
    engine = OpenRouterVoice(transport=transport_of(b""))
    spoken = engine.speak("hello", tmp_path / "seg.wav")
    assert not spoken.ok
    assert "no audio" in spoken.reason


def test_undecodable_bytes_do_not_leave_a_file_behind(keyed, tmp_path):
    """ffmpeg refusing the reply is a failed segment, not a zero-byte wav that
    concatenates cleanly and plays as nothing."""
    engine = OpenRouterVoice(transport=transport_of(b"this is not audio"))
    out = tmp_path / "seg.wav"
    spoken = engine.speak("hello", out)
    assert not spoken.ok
    assert "decode" in spoken.reason
    assert list(tmp_path.glob("*.mp3")) == []


def test_a_transport_failure_becomes_a_reason_not_an_exception(keyed, tmp_path):
    def boom(payload):
        raise SpeechFailed("rate limited by OpenRouter: free tier, 50/day")

    engine = OpenRouterVoice(transport=boom)
    spoken = engine.speak("hello", tmp_path / "seg.wav")
    assert not spoken.ok
    assert "50/day" in spoken.reason


# -- error reporting --------------------------------------------------------


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(body))


def test_a_429_says_it_was_rate_limited_and_what_the_cap_was():
    """The body is the whole difference between `rerun tomorrow` and a bug hunt."""
    reason = _http_reason(
        _http_error(429, b'{"error":{"message":"free-tier daily limit reached"}}'), None
    )
    assert "rate limited" in reason
    assert "free-tier daily limit reached" in reason


def test_an_error_reason_never_leaks_the_key():
    key = "sk-or-v1-SECRET"
    reason = _http_reason(_http_error(401, f'{{"error":"bad key {key}"}}'.encode()), key)
    assert key not in reason
    assert "[REDACTED]" in reason


def test_a_two_hundred_carrying_json_does_not_leak_the_key_either(keyed, tmp_path, monkeypatch):
    """The error-inside-a-200 path is easy to forget when the redaction lives on
    the two obvious failure branches."""

    class Reply:
        headers = {"Content-Type": "application/json"}

        def read(self):
            return f'{{"error":"bad key {keyed}"}}'.encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Reply())
    spoken = OpenRouterVoice().speak("hello", tmp_path / "seg.wav")
    assert not spoken.ok
    assert keyed not in spoken.reason
    assert "[REDACTED]" in spoken.reason


def test_an_unreadable_body_still_reports_the_status():
    class Unreadable(urllib.error.HTTPError):
        def read(self):
            raise OSError("connection reset")

    reason = _http_reason(Unreadable("u", 502, "m", {}, None), None)
    assert "502" in reason


def test_a_plain_text_body_is_reported_as_is():
    assert _detail(b"upstream exploded") == "upstream exploded"


def test_a_nested_error_message_is_pulled_out():
    assert _detail(b'{"error":{"message":"no such voice: fred","code":400}}') == "no such voice: fred"


# -- the default ------------------------------------------------------------


def test_the_default_model_is_the_free_one():
    """A default that costs money per paragraph makes `kreb audio` something you
    think twice about running."""
    assert DEFAULT_MODEL.endswith(":free")
    assert OpenRouterVoice().model == DEFAULT_MODEL


def test_the_command_defaults_to_the_same_voice_the_engine_does():
    """Two defaults that can disagree eventually do. PLAN.md documents one of
    them, and the one people actually get is the parser's."""
    from kreb.cli import build_parser

    args = build_parser().parse_args(["audio", "doc.json"])
    assert args.voice == DEFAULT_MODEL
    assert isinstance(engine_for(args), OpenRouterVoice)


# -- CLI dispatch -----------------------------------------------------------


def _args(voice: str, **extra) -> Namespace:
    return Namespace(
        voice=voice,
        voice_name=extra.get("voice_name", ""),
        speed=extra.get("speed", 1.0),
        style=extra.get("style", "monologue"),
        host_voice=extra.get("host_voice", ""),
        host_voice_name=extra.get("host_voice_name", ""),
    )


def test_silence_selects_the_placeholder_engine():
    from kreb.tts.silence import SilenceEngine

    assert isinstance(engine_for(_args("silence")), SilenceEngine)


def test_an_onnx_path_selects_piper():
    from kreb.tts.piper import PiperEngine

    engine = engine_for(_args("voices/en_US-amy.onnx"))
    assert isinstance(engine, PiperEngine)
    assert engine.voice == Path("voices/en_US-amy.onnx")


def test_an_onnx_path_wins_over_the_slash_rule():
    """A piper path almost always contains a slash. Read as a model id it would
    be posted to a hosted API, and the failure would arrive as a network error
    about a filesystem path."""
    from kreb.tts.piper import PiperEngine

    assert isinstance(engine_for(_args("/opt/voices/a/b.onnx")), PiperEngine)


def test_a_slashed_name_selects_the_hosted_engine():
    engine = engine_for(_args("deepgram/flux-tts:free", voice_name="nova", speed=1.2))
    assert isinstance(engine, OpenRouterVoice)
    assert engine.model == "deepgram/flux-tts:free"
    assert engine.voice == "nova"
    assert engine.speed == 1.2


def test_an_unrecognized_voice_is_refused_by_name():
    """Guessing between a typo'd filename and an unfamiliar model id gets one of
    them wrong, and both surface much later as something unrelated."""
    with pytest.raises(ValueError) as caught:
        engine_for(_args("amy"))
    assert "silence" in str(caught.value) and ".onnx" in str(caught.value)


def test_the_hosted_engine_satisfies_the_port():
    from kreb.tts.base import SpeechEngine

    assert isinstance(OpenRouterVoice(), SpeechEngine)


@needs_ffmpeg
def test_switching_to_a_hosted_voice_invalidates_the_cache(keyed):
    """The seam this whole `identity` mechanism exists to prevent: re-narrating
    with a different engine must not serve the old engine's audio."""
    from kreb.render.audio import segment_key
    from kreb.render.narration import Segment
    from kreb.tts.silence import SilenceEngine

    segment = Segment(
        id="s1",
        section_id="sec",
        role="body",
        text="hello there",
        beat_order=0,
        confidence="verified",
        kind="structure",
    )
    assert segment_key(segment, SilenceEngine()) != segment_key(segment, OpenRouterVoice())
