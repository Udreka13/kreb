"""Speech synthesis as a port, with the adapters this project ships."""

from kreb.tts.base import Availability, SpeechEngine, Spoken
from kreb.tts.piper import PiperEngine
from kreb.tts.silence import SilenceEngine

__all__ = [
    "Availability",
    "PiperEngine",
    "SilenceEngine",
    "SpeechEngine",
    "Spoken",
]
