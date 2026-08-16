"""Ceilings and the stop decision.

**No default cap.** The engine does not silently truncate research to hit a
number nobody chose — a document that stops halfway because of an invented
default is worse than an expensive one the user agreed to. Every ceiling here is
optional, and absent means unlimited while still accounting every row.

**A ceiling cannot be exact, and pretending otherwise is the bug.** The cost of
a call is not knowable until it returns, so `max_per_run` means *no new call is
started once the ceiling is passed*. Overshoot is bounded by the single call in
flight. That is the honest guarantee, and it is the one that gets tested.

Two stop mechanisms exist for one reason: a `BudgetExceeded` raised in the
middle of writing a section throws that section away, which is the "killed run
that produced nothing" outcome at section granularity. So `should_stop()` is the
mechanism — checked by the research loop *between* units, letting completed
sections persist and the run stay resumable — and the pre-call guard is only a
backstop for a caller that forgot to ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kreb.budget.ledger import Ledger, Phase


class BudgetExceeded(RuntimeError):
    """Raised by the backstop when a call is attempted past the ceiling."""

    def __init__(self, spent: float, ceiling: float, scope: str) -> None:
        super().__init__(
            f"{scope} budget exhausted: ${spent:.4f} spent against a ${ceiling:.4f} ceiling"
        )
        self.spent = spent
        self.ceiling = ceiling
        self.scope = scope


@dataclass(frozen=True)
class Decision:
    """Why the run should or should not continue, in words a user can act on."""

    stop: bool
    warn: bool
    reason: str = ""
    spent: float = 0.0
    ceiling: float | None = None

    @property
    def remaining(self) -> float | None:
        return None if self.ceiling is None else max(0.0, self.ceiling - self.spent)


@dataclass(frozen=True)
class Budget:
    """Ceilings in currency, all optional.

    `max_per_phase` lets renderers be capped separately from research, which is
    the split users actually want: a large ceiling for "think hard about this
    codebase" and a small one for "read it aloud".
    """

    max_per_run: float | None = None
    max_per_day: float | None = None
    max_per_phase: dict[Phase, float] | None = None
    # Fraction of a ceiling at which to warn. Only meaningful with a ceiling.
    warn_at: float = 0.8

    def decide(
        self, ledger: Ledger, *, phase: Phase | None = None, now: datetime | None = None
    ) -> Decision:
        """Whether to start another unit of work."""
        run_spent = ledger.total()

        if self.max_per_run is not None and run_spent >= self.max_per_run:
            return Decision(
                stop=True,
                warn=True,
                reason="run ceiling reached; completed work has been saved and the "
                "run can be resumed with a higher ceiling",
                spent=run_spent,
                ceiling=self.max_per_run,
            )

        if self.max_per_day is not None:
            spent_today = ledger.today(now=now)
            if spent_today >= self.max_per_day:
                return Decision(
                    stop=True,
                    warn=True,
                    reason=(
                        "daily ceiling reached"
                        if ledger.persistent
                        else "daily ceiling reached within this run (the ledger is "
                        "in-memory, so spend from earlier runs is not counted)"
                    ),
                    spent=spent_today,
                    ceiling=self.max_per_day,
                )

        if phase is not None and self.max_per_phase:
            ceiling = self.max_per_phase.get(phase)
            if ceiling is not None:
                spent = ledger.total(phase=phase)
                if spent >= ceiling:
                    return Decision(
                        stop=True,
                        warn=True,
                        reason=f"{phase} ceiling reached; other phases may continue",
                        spent=spent,
                        ceiling=ceiling,
                    )

        ceiling = self.max_per_run
        if ceiling is not None and run_spent >= ceiling * self.warn_at:
            return Decision(
                stop=False,
                warn=True,
                reason=f"{run_spent / ceiling:.0%} of the run ceiling spent",
                spent=run_spent,
                ceiling=ceiling,
            )

        return Decision(stop=False, warn=False, spent=run_spent, ceiling=ceiling)

    def should_stop(self, ledger: Ledger, *, phase: Phase | None = None) -> bool:
        return self.decide(ledger, phase=phase).stop

    def guard(self, ledger: Ledger, *, phase: Phase | None = None) -> None:
        """Backstop: refuse to start a call past a ceiling.

        Deliberately *not* the primary mechanism. Reaching this means a loop
        did not check `should_stop` between units, so raising here is the
        lesser evil — it stops the spend, at the cost of the unit in flight.
        """
        decision = self.decide(ledger, phase=phase)
        if decision.stop:
            raise BudgetExceeded(
                spent=decision.spent,
                ceiling=decision.ceiling if decision.ceiling is not None else 0.0,
                scope=phase or "run",
            )

    @property
    def unlimited(self) -> bool:
        return (
            self.max_per_run is None
            and self.max_per_day is None
            and not self.max_per_phase
        )
