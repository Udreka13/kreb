"""The seam that makes spending without recording structurally impossible.

Every inference call in the pipeline goes through here, and this is the only
place that knows how to reach a `Provider`. The ledger write is not a courtesy
the caller performs afterwards — it happens on the way out of the call, on every
path including the failing ones, the same way `store/` writes provenance before
the artifact it describes.

The property being defended: **a retried generation is charged for each
attempt.** The research loop generates, validates, and regenerates on rejection.
Each of those is a real completion that was really billed. Leaving the charge to
the caller means the caller eventually forgets on one path, the ceiling
under-counts by up to 3×, and nothing in the output reveals it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kreb.budget.ledger import Charge, Ledger, Phase
from kreb.budget.policy import Budget
from kreb.provider.base import Provider, ProviderError
from kreb.provider.types import Completion, Request, Usage


@dataclass
class MeteredProvider:
    """A `Provider` that charges the ledger and honours the budget.

    Satisfies `Provider` itself, so it composes and nothing downstream needs to
    know whether it is talking to a metered or a raw provider.
    """

    inner: Provider
    ledger: Ledger
    budget: Budget = field(default_factory=Budget)
    phase: Phase = "research"

    def model_for(self, role: str) -> str:
        return self.inner.model_for(role)

    def complete(self, request: Request) -> Completion:
        """Guard, call, charge — in that order, with charging unconditional."""
        self.budget.guard(self.ledger, phase=self.phase)

        try:
            completion = self.inner.complete(request)
        except ProviderError:
            # A transport failure produced no generation, so there is nothing to
            # charge. Deliberately not recorded as a zero-cost row: that would
            # inflate the attempt count with calls that never reached a model
            # and make `wasted` unreadable.
            raise

        self.ledger.charge(
            Charge.from_usage(
                phase=self.phase,
                unit=request.unit,
                role=request.role,
                model=completion.model,
                usage=completion.usage,
                attempt=completion.attempt,
            )
        )
        return completion

    # -- the retry-and-validate loop --------------------------------------

    def complete_validated(
        self,
        request: Request,
        validate,
        *,
        max_attempts: int = 3,
    ) -> tuple[Completion | None, list[str]]:
        """Generate until `validate` accepts, charging every attempt.

        `validate(text) -> list[str]` returns the reasons the output was
        rejected, empty when acceptable.

        Returns the accepted completion and the rejection reasons seen along the
        way — the reasons are returned rather than discarded because a section
        that took three tries is a fact about the run, and the caller may want
        to record it or give up rather than ship the last attempt.

        Note what this loop does *not* do: it never quietly returns the final
        rejected attempt as though it passed. A caller that gets `None` must
        decide, because "the validator kept saying no so we shipped it anyway"
        is how a validation rule becomes decorative.
        """
        reasons: list[str] = []
        for attempt in range(1, max_attempts + 1):
            self.budget.guard(self.ledger, phase=self.phase)
            completion = self.inner.complete(request)

            # From here the generation exists and the provider has billed for
            # it, so the charge must survive anything `validate` does —
            # including raising. Recording it only on the paths where
            # validation returned normally means a validator bug shows up as
            # free inference, which is the exact under-count this class exists
            # to prevent.
            failures: list[str] = []
            try:
                failures = list(validate(completion.text))
            except BaseException:
                failures = ["the validator raised while checking this generation"]
                raise
            finally:
                self.ledger.charge(
                    Charge.from_usage(
                        phase=self.phase,
                        unit=request.unit,
                        role=request.role,
                        model=completion.model,
                        usage=completion.usage,
                        attempt=attempt,
                        failed=bool(failures),
                    )
                )

            if not failures:
                return completion, reasons
            reasons.extend(failures)
        return None, reasons

    def record_cache_hit(self, request: Request, *, model: str = "") -> Charge:
        """Record that a unit was served from the store without an inference call.

        A row with cost 0, not an absent row. A re-run costing nearly nothing
        should be visible as *work that was reused*, which an empty ledger
        cannot express — it looks identical to a run that did nothing.
        """
        return self.ledger.charge(
            Charge.from_usage(
                phase=self.phase,
                unit=request.unit,
                role=request.role,
                model=model or self.model_for(request.role),
                usage=Usage(),
                cached=True,
            )
        )
