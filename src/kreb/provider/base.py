"""The inference port, and the errors it may raise.

Sync, deliberately. architecture.md §7 originally specified asyncio for network
work; that is revised here, in the open, rather than drifted away from. Every
other subsystem — git, tree-sitter, d2, ffmpeg, piper — is synchronous
subprocess work, and the concurrency this pipeline needs is dozens of
simultaneous requests, not thousands. A `ThreadPoolExecutor` covers that
completely, while an async port would make the codebase two-coloured and force
`research/`, `render/` and `cli/` to follow. The port stays a Protocol, so an
async implementation behind a thread wrapper remains possible.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kreb.provider.types import Completion, Request


class ProviderError(RuntimeError):
    """Base for transport failures.

    Subclasses never carry the API key. Error strings reach logs, exception
    reports and — through a failed section — potentially a document, so the
    credential must not be in them.
    """


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ContextTooLong(ProviderError):
    """The request did not fit. Retrying it unchanged cannot help."""


class ProviderRefused(ProviderError):
    """The model declined. Distinct from a transport error: not retryable."""


@runtime_checkable
class Provider(Protocol):
    """Anything that can turn a `Request` into a `Completion`."""

    def complete(self, request: Request) -> Completion: ...

    def model_for(self, role: str) -> str: ...
