"""OpenRouter transport.

Chosen as the v1 provider for one specific reason: **it returns the actual cost
of each generation in the response body**, which is what makes the budget
measured rather than estimated. Verified against the usage-accounting docs on
2026-08-16 — no request parameter is needed (the old `usage: {include: true}`
and `stream_options.include_usage` are deprecated and have no effect), and for
streaming responses the usage object arrives in the final SSE chunk.

The response shape this parses, pinned to that observation:

    {"usage": {"prompt_tokens": 194, "completion_tokens": 2,
               "cost": 0.95,
               "cost_details": {"upstream_inference_cost": 19},
               "prompt_tokens_details": {"cached_tokens": 0, ...},
               "completion_tokens_details": {"reasoning_tokens": 0}}}

Uses stdlib `urllib` rather than `httpx`/`requests`. The dependency list is two
packages plus pydantic, and one POST with retries does not justify a third.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from kreb.provider.base import (
    ContextTooLong,
    ProviderError,
    ProviderRefused,
    RateLimited,
)
from kreb.provider.types import Completion, Message, ModelPricing, Request, Role, Usage

API_ROOT = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 180.0

# Role -> model, as a starting point only. Exact IDs are pinned in config at
# release: OpenRouter identifiers move, and a stale default baked into source is
# worse than no default because it fails at runtime rather than at config time.
PROFILES: dict[str, dict[Role, str]] = {
    "budget": {
        "research": "deepseek/deepseek-v4-flash-0731",
        "narrate": "deepseek/deepseek-v4-flash-0731",
        "visualize": "deepseek/deepseek-v4-flash-0731",
        "mechanical": "deepseek/deepseek-v4-flash-0731",
    },
    "balanced": {
        "research": "anthropic/claude-sonnet-5",
        "narrate": "deepseek/deepseek-v4-flash-0731",
        "visualize": "deepseek/deepseek-v4-flash-0731",
        "mechanical": "deepseek/deepseek-v4-flash-0731",
    },
    "max": {
        "research": "anthropic/claude-opus-5",
        "narrate": "anthropic/claude-sonnet-5",
        "visualize": "anthropic/claude-sonnet-5",
        "mechanical": "deepseek/deepseek-v4-flash-0731",
    },
}


@dataclass
class OpenRouterProvider:
    """Read-only-ish HTTP client for OpenRouter's chat completions."""

    api_key: str = field(repr=False)
    profile: str = "balanced"
    models: dict[Role, str] = field(default_factory=dict)
    pricing: dict[str, ModelPricing] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = 3
    referer: str = "https://github.com/Udreka13/kreb"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("an OpenRouter API key is required")
        merged = dict(PROFILES.get(self.profile, PROFILES["balanced"]))
        merged.update(self.models)
        self.models = merged

    def __repr__(self) -> str:
        """Never render the key.

        `repr` lands in tracebacks, logs and pytest output. The dataclass field
        is already `repr=False`; this makes it explicit and survives someone
        adding a field later.
        """
        return f"OpenRouterProvider(profile={self.profile!r}, models={len(self.models)})"

    def model_for(self, role: str) -> str:
        return self.models.get(role) or self.models["mechanical"]

    # -- the call ----------------------------------------------------------

    def complete(self, request: Request) -> Completion:
        """One generation, retrying transport failures with backoff.

        Retries here are for *transport* only — a 429 or a 5xx, where the same
        request may succeed unchanged. A rejected-by-validation retry is a
        different thing entirely and belongs to the caller, because only the
        caller can charge each attempt to the ledger.
        """
        model = request.model or self.model_for(request.role)
        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.stop:
            payload["stop"] = list(request.stop)

        last: ProviderError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                body = self._post("/chat/completions", payload)
                return _to_completion(body, model=model, attempt=attempt, pricing=self.pricing)
            except RateLimited as exc:
                last = exc
                if attempt == self.max_retries:
                    break
                time.sleep(exc.retry_after if exc.retry_after else min(2**attempt, 30))
            except (ContextTooLong, ProviderRefused):
                raise  # retrying unchanged cannot help
            except ProviderError as exc:
                last = exc
                if attempt == self.max_retries:
                    break
                time.sleep(min(2**attempt, 30))
        raise last or ProviderError("request failed with no diagnosis")

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{API_ROOT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": self.referer,
                "X-Title": "kreb",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _classify(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"network failure: {exc}") from None


def _classify(exc: urllib.error.HTTPError) -> ProviderError:
    """Map an HTTP failure to a typed error, without leaking the key.

    The request headers carry the credential, so nothing derived from the
    request may go into the message — only the status and the response body,
    truncated.
    """
    try:
        detail = exc.read().decode("utf-8", "replace")[:400]
    except Exception:
        detail = ""
    lowered = detail.lower()

    if exc.code == 429:
        retry_after = None
        header = (getattr(exc, "headers", None) or {}).get("retry-after")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        return RateLimited(f"rate limited: {detail}", retry_after=retry_after)
    if exc.code == 400 and ("context" in lowered or "too long" in lowered or "maximum" in lowered):
        return ContextTooLong(f"request did not fit: {detail}")
    if exc.code in (401, 403):
        return ProviderError(f"authentication rejected (HTTP {exc.code})")
    if exc.code == 402:
        return ProviderError("OpenRouter account has insufficient credit")
    return ProviderError(f"HTTP {exc.code}: {detail}")


def _to_completion(
    body: dict, *, model: str, attempt: int, pricing: dict[str, ModelPricing]
) -> Completion:
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError("response contained no choices")

    first = choices[0]
    message = first.get("message") or {}
    text = message.get("content") or ""
    finish = first.get("finish_reason") or "stop"

    if finish == "content_filter":
        raise ProviderRefused("generation was filtered")
    if finish == "length":
        # Not an error — the caller may want the truncated text — but it must be
        # visible, because a section silently cut mid-sentence reads as a
        # complete thought that happens to be wrong.
        pass

    usage = _to_usage(body.get("usage") or {}, model=model, pricing=pricing)
    return Completion(
        text=text,
        usage=usage,
        model=body.get("model") or model,
        attempt=attempt,
        finish_reason=finish,
        raw=body,
    )


def _to_usage(raw: dict, *, model: str, pricing: dict[str, ModelPricing]) -> Usage:
    """Read the reported cost; estimate only when there is none, and say so."""
    prompt_tokens = int(raw.get("prompt_tokens") or 0)
    completion_tokens = int(raw.get("completion_tokens") or 0)
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}

    reported = raw.get("cost")
    if reported is None:
        table = pricing.get(model)
        cost = table.estimate(prompt_tokens, completion_tokens) if table else 0.0
        estimated = True
    else:
        cost = float(reported)
        estimated = False

    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        cost=cost,
        cost_is_estimated=estimated,
    )


def parse_usage(raw: dict, *, model: str = "", pricing: dict[str, ModelPricing] | None = None):
    """Public entry point for the usage parser, so it can be tested directly."""
    return _to_usage(raw, model=model, pricing=pricing or {})


def messages(system: str, user: str) -> tuple[Message, ...]:
    return (Message("system", system), Message("user", user))
