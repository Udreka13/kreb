"""Inference request and response types.

`Usage.cost` is the number the whole budget system rests on, and it is
**measured, not estimated**. OpenRouter returns the actual charge for each
generation in the response body, which is the specific reason it is the v1
provider — a ceiling computed from token counts times a price table is guesswork
that drifts the moment a model's pricing changes or a request gets routed to a
different upstream.

When a provider cannot report cost, that is recorded as `cost_is_estimated`
rather than silently substituted. A budget that cannot tell a measured spend
from a guessed one is not a budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Models are configured by role, not globally: the research pass needs long
# context and reasoning, slide copy is nearly mechanical, and paying frontier
# prices for the latter is most of how a run gets expensive.
Role = Literal["research", "narrate", "visualize", "mechanical"]

ROLES: tuple[Role, ...] = ("research", "narrate", "visualize", "mechanical")


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Request:
    """One inference call. Immutable so a retry cannot mutate what it retried."""

    messages: tuple[Message, ...]
    role: Role = "mechanical"
    model: str = ""
    max_tokens: int | None = None
    temperature: float = 0.0
    # Free-text label for what this call is *for* — a section id, "outline",
    # "stitch". It becomes the ledger's unit column, which is what makes the
    # retry-attempt instrumentation legible per section rather than in aggregate.
    unit: str = ""
    response_format: dict | None = None
    stop: tuple[str, ...] = ()


@dataclass(frozen=True)
class Usage:
    """What a generation consumed and what it cost.

    Field names mirror OpenRouter's response body so the mapping is inspectable
    rather than reinterpreted: `usage.cost`, `usage.prompt_tokens_details.
    cached_tokens`, `usage.completion_tokens_details.reasoning_tokens`.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    cost_is_estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost=self.cost + other.cost,
            cost_is_estimated=self.cost_is_estimated or other.cost_is_estimated,
        )


@dataclass(frozen=True)
class Completion:
    """A generation, plus how many tries it took to get here."""

    text: str
    usage: Usage
    model: str
    # 1-based. Anything above 1 means earlier attempts were made *and charged*;
    # see budget.ledger for why that must reach the ledger.
    attempt: int = 1
    finish_reason: str = "stop"
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ModelPricing:
    """Per-token prices, used only for preflight estimates and fallbacks.

    Never used to compute a charge when the provider reported one. Prices are
    per token, matching OpenRouter's `/models` payload.
    """

    prompt: float = 0.0
    completion: float = 0.0

    def estimate(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens * self.prompt + completion_tokens * self.completion
