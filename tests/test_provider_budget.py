"""Tests for inference transport, spend accounting and ceilings.

The load-bearing test in this file is `test_every_retry_attempt_is_charged`.
Everything else guards a supporting property; that one guards the number the
ceiling is enforced against. A pipeline that regenerates rejected sections and
records only the successful attempt under-reports by up to 3×, and nothing in
the output reveals it — the run simply costs three times what the user agreed to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from kreb.budget.ledger import RENDER, RESEARCH, Charge, Ledger
from kreb.budget.policy import Budget, BudgetExceeded
from kreb.config.secrets import (
    MissingCredential,
    SecretInConfig,
    reject_secrets_in_config,
    resolve_api_key,
)
from kreb.provider.base import ContextTooLong, ProviderError, RateLimited
from kreb.provider.metered import MeteredProvider
from kreb.provider.openrouter import OpenRouterProvider, parse_usage
from kreb.provider.types import Message, ModelPricing, Request, Usage

# The exact shape documented at openrouter.ai/docs/use-cases/usage-accounting,
# read 2026-08-16. Pinned as a fixture so a parser change that stops reading
# `usage.cost` fails here rather than in production, where it would silently
# make every generation free.
OPENROUTER_BODY = {
    "id": "gen-123",
    "model": "anthropic/claude-sonnet-5",
    "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {
        "completion_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 7},
        "cost": 0.95,
        "cost_details": {"upstream_inference_cost": 19},
        "prompt_tokens": 194,
        "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 100},
        "total_tokens": 196,
    },
}


def _request(unit="s1", role="research"):
    return Request(messages=(Message("user", "hi"),), role=role, unit=unit)


class FakeProvider:
    """A provider with a scripted sequence of outputs."""

    def __init__(self, *, texts=("ok",), cost=1.0, model="test/model"):
        self.texts = list(texts)
        self.cost = cost
        self.model = model
        self.calls = 0

    def model_for(self, role):
        return self.model

    def complete(self, request):
        from kreb.provider.types import Completion

        self.calls += 1
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)]
        return Completion(
            text=text,
            usage=Usage(prompt_tokens=10, completion_tokens=5, cost=self.cost),
            model=self.model,
        )


# -- usage parsing ---------------------------------------------------------


def test_reported_cost_is_used_verbatim():
    usage = parse_usage(OPENROUTER_BODY["usage"])
    assert usage.cost == 0.95
    assert usage.cost_is_estimated is False
    assert usage.prompt_tokens == 194
    assert usage.completion_tokens == 2
    assert usage.cached_tokens == 3
    assert usage.reasoning_tokens == 7


def test_missing_cost_falls_back_to_pricing_and_says_so():
    """A guessed cost must never be indistinguishable from a measured one."""
    usage = parse_usage(
        {"prompt_tokens": 1000, "completion_tokens": 500},
        model="m",
        pricing={"m": ModelPricing(prompt=0.001, completion=0.002)},
    )
    assert usage.cost == pytest.approx(1000 * 0.001 + 500 * 0.002)
    assert usage.cost_is_estimated is True


def test_zero_reported_cost_is_not_treated_as_missing():
    """A genuinely free generation reports 0.0, which is not the same as absent."""
    usage = parse_usage({"prompt_tokens": 5, "completion_tokens": 1, "cost": 0})
    assert usage.cost == 0.0
    assert usage.cost_is_estimated is False


def test_usage_adds():
    total = Usage(prompt_tokens=1, cost=0.5) + Usage(completion_tokens=2, cost=0.25)
    assert total.cost == 0.75
    assert total.total_tokens == 3


# -- the ledger ------------------------------------------------------------


def test_every_retry_attempt_is_charged():
    """The property the ceiling depends on.

    Two rejected generations and one accepted one is three billed completions.
    Recording only the accepted attempt makes the run cost 3x what the ledger
    claims, and the ceiling enforces a number that is not money.
    """
    ledger = Ledger()
    provider = MeteredProvider(
        inner=FakeProvider(texts=("bad", "bad", "good"), cost=1.0), ledger=ledger
    )

    completion, reasons = provider.complete_validated(
        _request(), lambda text: [] if text == "good" else ["rejected"], max_attempts=3
    )

    assert completion is not None and completion.text == "good"
    assert len(ledger.rows) == 3
    assert ledger.total() == pytest.approx(3.0)
    assert ledger.wasted() == pytest.approx(2.0)
    assert [r.attempt for r in ledger.rows] == [1, 2, 3]
    assert [r.failed for r in ledger.rows] == [True, True, False]
    assert len(reasons) == 2


def test_exhausted_validation_returns_none_rather_than_the_last_attempt():
    """Shipping the final rejected attempt is how a rule becomes decorative."""
    ledger = Ledger()
    provider = MeteredProvider(inner=FakeProvider(texts=("bad",), cost=0.5), ledger=ledger)

    completion, reasons = provider.complete_validated(
        _request(), lambda text: ["always rejected"], max_attempts=3
    )

    assert completion is None
    assert len(reasons) == 3
    assert ledger.total() == pytest.approx(1.5)
    assert all(r.failed for r in ledger.rows)


def test_plain_complete_charges_too():
    ledger = Ledger()
    provider = MeteredProvider(inner=FakeProvider(cost=2.0), ledger=ledger)
    provider.complete(_request())
    assert ledger.total() == pytest.approx(2.0)


def test_cache_hits_are_zero_cost_rows_not_absent_rows():
    """An empty ledger cannot express "work was reused"; it looks like idleness."""
    ledger = Ledger()
    provider = MeteredProvider(inner=FakeProvider(), ledger=ledger)
    provider.record_cache_hit(_request(unit="s9"))

    assert len(ledger.rows) == 1
    assert ledger.total() == 0.0
    assert ledger.rows[0].cached is True
    assert ledger.attempts() == 0  # a cache hit is not an inference call


def test_transport_failures_are_not_charged():
    """No generation happened, so there is nothing to bill."""

    class Broken:
        def model_for(self, role):
            return "m"

        def complete(self, request):
            raise ProviderError("network down")

    ledger = Ledger()
    provider = MeteredProvider(inner=Broken(), ledger=ledger)
    with pytest.raises(ProviderError):
        provider.complete(_request())
    assert ledger.rows == []


def test_phases_are_accounted_separately():
    ledger = Ledger()
    MeteredProvider(inner=FakeProvider(cost=3.0), ledger=ledger, phase=RESEARCH).complete(
        _request()
    )
    MeteredProvider(inner=FakeProvider(cost=1.0), ledger=ledger, phase=RENDER).complete(_request())

    assert ledger.total(phase=RESEARCH) == pytest.approx(3.0)
    assert ledger.total(phase=RENDER) == pytest.approx(1.0)
    assert ledger.total() == pytest.approx(4.0)
    assert ledger.by_phase() == {RESEARCH: pytest.approx(3.0), RENDER: pytest.approx(1.0)}


def test_spend_is_attributed_per_unit():
    ledger = Ledger()
    provider = MeteredProvider(inner=FakeProvider(cost=1.0), ledger=ledger)
    provider.complete(_request(unit="intro"))
    provider.complete(_request(unit="intro"))
    provider.complete(_request(unit="auth"))

    assert ledger.by_unit() == {"intro": pytest.approx(2.0), "auth": pytest.approx(1.0)}


# -- persistence -----------------------------------------------------------


def test_ledger_survives_a_restart(tmp_path):
    """Losing the tail of the ledger silently resets the daily ceiling."""
    path = tmp_path / ".kreb" / "spend.jsonl"
    first = Ledger(path)
    MeteredProvider(inner=FakeProvider(cost=2.5), ledger=first).complete(_request())

    reopened = Ledger(path)
    assert reopened.total() == pytest.approx(2.5)
    assert reopened.persistent is True


def test_a_truncated_final_line_does_not_break_the_ledger(tmp_path):
    """A hard kill mid-write must not make the whole history unreadable."""
    path = tmp_path / "spend.jsonl"
    ledger = Ledger(path)
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=1.0))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"phase": "research", "unit": "b", "cos')

    reopened = Ledger(path)
    assert reopened.total() == pytest.approx(1.0)
    assert len(reopened.rows) == 1


def test_in_memory_ledger_reports_that_it_is_not_persistent():
    assert Ledger().persistent is False


def test_a_validator_that_raises_still_charges_the_generation():
    """Found in review. The completion exists and the provider has billed for
    it; recording the charge only on paths where validation returned normally
    turns a validator bug into free inference."""
    ledger = Ledger()
    provider = MeteredProvider(inner=FakeProvider(cost=5.0), ledger=ledger)

    def exploding(text):
        raise ValueError("validator exploded")

    with pytest.raises(ValueError):
        provider.complete_validated(_request(), exploding)

    assert ledger.total() == pytest.approx(5.0)
    assert ledger.rows[0].failed is True


def test_two_ledgers_on_one_file_see_each_others_spend(tmp_path):
    """Found in review. Research and render are metered separately but share a
    file; without a resync each held its construction-time snapshot, so a $10
    daily ceiling let two phases spend $8 apiece and neither stopped."""
    path = tmp_path / "spend.jsonl"
    research = Ledger(path)
    render = Ledger(path)

    research.charge(Charge(phase=RESEARCH, unit="s1", role="research", model="m", cost=8.0))
    render.charge(Charge(phase=RENDER, unit="r1", role="mechanical", model="m", cost=8.0))

    assert research.total() == pytest.approx(16.0)
    assert render.total() == pytest.approx(16.0)

    budget = Budget(max_per_day=10.0)
    assert budget.should_stop(research) is True
    assert budget.should_stop(render) is True


def test_rows_are_not_double_counted_after_a_resync(tmp_path):
    path = tmp_path / "spend.jsonl"
    a = Ledger(path)
    a.charge(Charge(phase=RESEARCH, unit="x", role="research", model="m", cost=1.0))
    a.total()
    a.total()
    a.charge(Charge(phase=RESEARCH, unit="y", role="research", model="m", cost=1.0))
    assert a.total() == pytest.approx(2.0)
    assert len(a.rows) == 2
    assert Ledger(path).total() == pytest.approx(2.0)


# -- ceilings --------------------------------------------------------------


def test_no_ceiling_means_no_stop():
    """The engine does not truncate research to hit a number nobody chose."""
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=9999.0))
    budget = Budget()
    assert budget.unlimited is True
    assert budget.should_stop(ledger) is False


def test_run_ceiling_stops_between_units():
    ledger = Ledger()
    budget = Budget(max_per_run=1.0)
    provider = MeteredProvider(inner=FakeProvider(cost=0.6), ledger=ledger, budget=budget)

    provider.complete(_request())
    assert budget.should_stop(ledger) is False  # 0.6 < 1.0
    provider.complete(_request())
    assert budget.should_stop(ledger) is True  # 1.2 >= 1.0


def test_overshoot_is_bounded_by_one_call_and_not_pretended_away():
    """A call's cost is unknowable until it returns, so the ceiling means
    "start no new call once passed". The guarantee is bounded overshoot, and
    the test asserts the bound rather than an exactness that cannot hold."""
    ledger = Ledger()
    budget = Budget(max_per_run=1.0)
    provider = MeteredProvider(inner=FakeProvider(cost=0.9), ledger=ledger, budget=budget)

    calls = 0
    while not budget.should_stop(ledger):
        provider.complete(_request())
        calls += 1

    assert ledger.total() > 1.0  # overshot, as it must
    assert ledger.total() <= 1.0 + 0.9  # by no more than one call
    assert calls == 2


def test_guard_is_a_backstop_that_refuses_to_spend_past_the_ceiling():
    ledger = Ledger()
    budget = Budget(max_per_run=1.0)
    provider = MeteredProvider(inner=FakeProvider(cost=2.0), ledger=ledger, budget=budget)

    provider.complete(_request())
    with pytest.raises(BudgetExceeded) as caught:
        provider.complete(_request())
    assert "2.0000" in str(caught.value) or "spent" in str(caught.value)
    assert ledger.attempts() == 1  # the second call never reached the provider


def test_per_phase_ceilings_are_independent():
    """A small ceiling for "read it aloud" must not stop "think hard"."""
    ledger = Ledger()
    budget = Budget(max_per_phase={RENDER: 0.5})
    MeteredProvider(inner=FakeProvider(cost=1.0), ledger=ledger, phase=RENDER).complete(_request())

    assert budget.should_stop(ledger, phase=RENDER) is True
    assert budget.should_stop(ledger, phase=RESEARCH) is False


def test_daily_ceiling_counts_only_today(tmp_path):
    path = tmp_path / "spend.jsonl"
    ledger = Ledger(path)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    ledger.charge(
        Charge(phase=RESEARCH, unit="old", role="research", model="m", cost=100.0, at=yesterday)
    )
    ledger.charge(Charge(phase=RESEARCH, unit="new", role="research", model="m", cost=1.0))

    budget = Budget(max_per_day=50.0)
    assert ledger.total() == pytest.approx(101.0)
    assert ledger.today() == pytest.approx(1.0)
    assert budget.should_stop(ledger) is False


def test_daily_ceiling_says_when_it_cannot_see_earlier_runs():
    """An in-memory ledger cannot enforce a cross-run ceiling. Say so."""
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=5.0))
    decision = Budget(max_per_day=1.0).decide(ledger)
    assert decision.stop is True
    assert "in-memory" in decision.reason


def test_phase_ceilings_are_enforced_even_without_a_phase_argument():
    """Found in review. Scoped to the phase asked about, a caller that omitted
    the argument skipped phase ceilings entirely — configured, reported as
    configured, never enforced."""
    ledger = Ledger()
    ledger.charge(Charge(phase=RENDER, unit="r", role="narrate", model="m", cost=5.0))
    budget = Budget(max_per_phase={RENDER: 1.0})

    assert budget.should_stop(ledger, phase=RENDER) is True
    assert budget.should_stop(ledger) is True  # was False before the fix


def test_a_day_only_budget_still_warns():
    """Found in review. `warn_at` watched only the run ceiling, so a budget
    configured with just a daily cap reached it with no warning at all."""
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=0.9))
    decision = Budget(max_per_day=1.0, warn_at=0.5).decide(ledger)
    assert decision.warn is True
    assert decision.stop is False
    assert "daily" in decision.reason


def test_ceilings_reports_everything_configured():
    budget = Budget(max_per_run=5.0, max_per_day=20.0, max_per_phase={RENDER: 1.0})
    assert budget.ceilings == {"run": 5.0, "day": 20.0, RENDER: 1.0}


def test_warning_fires_before_the_ceiling():
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=0.85))
    decision = Budget(max_per_run=1.0, warn_at=0.8).decide(ledger)
    assert decision.warn is True
    assert decision.stop is False
    assert decision.remaining == pytest.approx(0.15)


def test_stop_reason_tells_the_user_work_was_saved():
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=2.0))
    decision = Budget(max_per_run=1.0).decide(ledger)
    assert decision.stop is True
    assert "resumed" in decision.reason and "saved" in decision.reason


def test_estimated_share_is_visible():
    ledger = Ledger()
    ledger.charge(Charge(phase=RESEARCH, unit="a", role="research", model="m", cost=3.0))
    ledger.charge(
        Charge(
            phase=RESEARCH, unit="b", role="research", model="m", cost=1.0,
            cost_is_estimated=True,
        )
    )
    assert ledger.estimated_share() == pytest.approx(0.25)


# -- transport -------------------------------------------------------------


def test_provider_parses_a_real_response(monkeypatch):
    provider = OpenRouterProvider(api_key="sk-test-key-value")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(OPENROUTER_BODY).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    completion = provider.complete(_request())

    assert completion.text == "hello"
    assert completion.usage.cost == 0.95
    assert completion.model == "anthropic/claude-sonnet-5"


def test_rate_limits_are_retried_then_surface(monkeypatch):
    provider = OpenRouterProvider(api_key="k" * 20, max_retries=2)
    monkeypatch.setattr("time.sleep", lambda s: None)

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "slow down", {"retry-after": "0"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RateLimited):
        provider.complete(_request())


def test_context_overflow_is_not_retried(monkeypatch):
    """Sending the same oversized request again cannot help; it just costs time."""
    provider = OpenRouterProvider(api_key="k" * 20, max_retries=3)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError(
            "u", 400, "bad", {}, __import__("io").BytesIO(b"maximum context length exceeded")
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ContextTooLong):
        provider.complete(_request())
    assert len(calls) == 1


def test_role_selects_the_model():
    provider = OpenRouterProvider(api_key="k" * 20, profile="max")
    assert provider.model_for("research") != provider.model_for("mechanical")


def test_explicit_model_overrides_the_profile():
    provider = OpenRouterProvider(api_key="k" * 20, models={"research": "custom/model"})
    assert provider.model_for("research") == "custom/model"


# -- secret hygiene --------------------------------------------------------

SECRET = "sk-or-v1-abcdef0123456789abcdef"


def test_key_is_absent_from_repr():
    """`repr` reaches tracebacks, logs and pytest output."""
    provider = OpenRouterProvider(api_key=SECRET)
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)


def test_key_is_absent_from_transport_errors(monkeypatch):
    provider = OpenRouterProvider(api_key=SECRET, max_retries=1)

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "server error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ProviderError) as caught:
        provider.complete(_request())
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


def test_auth_failure_does_not_echo_the_key(monkeypatch):
    provider = OpenRouterProvider(api_key=SECRET, max_retries=1)

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ProviderError) as caught:
        provider.complete(_request())
    assert SECRET not in str(caught.value)


def test_ledger_rows_never_carry_the_key():
    ledger = Ledger()
    MeteredProvider(inner=FakeProvider(cost=1.0), ledger=ledger).complete(_request())
    assert SECRET not in json.dumps([r.__dict__ for r in ledger.rows])


@pytest.mark.parametrize(
    "config",
    [
        {"api_key": "sk-live"},
        {"provider": {"openrouter_api_key": "sk-live"}},
        {"a": {"b": {"token": "x"}}},
        {"providers": [{"secret_key": "x"}]},
        {"API-KEY": "x"},
    ],
)
def test_credentials_in_config_are_refused(config):
    """kreb.toml sits at the repo root and is meant to be committed."""
    with pytest.raises(SecretInConfig) as caught:
        reject_secrets_in_config(config)
    assert "OPENROUTER_API_KEY" in str(caught.value)


@pytest.mark.parametrize(
    "config",
    [
        {"budget": {"max_per_run": 2.0}},
        {"models": {"research": "anthropic/claude-sonnet-5"}},
        {"tokens_per_section": 800},
    ],
)
def test_ordinary_config_passes(config):
    reject_secrets_in_config(config)


def test_key_resolves_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    assert resolve_api_key() == SECRET


def test_missing_key_names_every_permitted_source(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KREB_API_KEY", raising=False)
    monkeypatch.setattr("kreb.config.secrets.from_keyring", lambda: None)
    with pytest.raises(MissingCredential) as caught:
        resolve_api_key()
    message = str(caught.value)
    assert "OPENROUTER_API_KEY" in message and "keyring" in message and "kreb.toml" in message


def test_empty_api_key_is_refused_at_construction():
    with pytest.raises(ValueError):
        OpenRouterProvider(api_key="")
