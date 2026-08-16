"""Spend accounting — one row per attempt, including the ones that failed.

**The failed attempts are the whole point.** The research loop generates a
section, validates it, and retries on rejection. Those rejected generations are
real completions that were really charged. An accounting that records only the
attempt that finally succeeded under-reports a three-retry section by 3×, and
the ceiling it enforces is a ceiling on something other than money.

That also makes this the instrumentation for validation laundering. If a section
needed four attempts to pass, the rows say so, and a rule that quietly costs
four generations per section is visible instead of being an invisible tax on
every run.

Rows are appended to JSONL as they are made, not written at the end. A run that
is killed must not lose its accounting — otherwise `max_per_day` silently resets
every time something crashes, which is the direction that costs money.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kreb.provider.types import Usage

# `budget/` sits *below* `provider/` in the layering: accounting knows nothing
# about transports, so `provider/metered.py` can depend on it without a cycle.
# Roles are plain strings here for the same reason — importing the Role literal
# would invert the dependency for no benefit, since the ledger never interprets
# a role, only records it.
Role = str

# Renderers are accounted separately from research: a user will reasonably want
# a large ceiling for "think hard about this codebase" and a small one for
# "read it aloud".
Phase = str

RESEARCH: Phase = "research"
RENDER: Phase = "render"


@dataclass(frozen=True)
class Charge:
    """One inference attempt, successful or not."""

    phase: Phase
    unit: str
    role: Role
    model: str
    cost: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    attempt: int = 1
    # A generation that was made and rejected. Still charged, still counted.
    failed: bool = False
    # Served from the artifact store without an inference call. Recorded as a
    # row with cost 0 rather than omitted, so a re-run's near-zero cost is
    # visible instead of looking like nothing happened.
    cached: bool = False
    cost_is_estimated: bool = False
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def from_usage(
        cls,
        *,
        phase: Phase,
        unit: str,
        role: Role,
        model: str,
        usage: "Usage",
        attempt: int = 1,
        failed: bool = False,
        cached: bool = False,
    ) -> Charge:
        return cls(
            phase=phase,
            unit=unit,
            role=role,
            model=model,
            cost=usage.cost,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            attempt=attempt,
            failed=failed,
            cached=cached,
            cost_is_estimated=usage.cost_is_estimated,
        )


class Ledger:
    """Append-only spend record, optionally persisted.

    Without a path this is in-memory and `max_per_day` cannot be enforced across
    runs — `persistent` reports which mode it is in so the caller can say so
    rather than implying a ceiling it is not keeping.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self.rows: list[Charge] = []
        self._offset = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._sync()

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def _sync(self) -> None:
        """Read any rows appended to the file since this ledger last looked.

        Research and render phases are metered through separate providers that
        share one file. Without this, each holds a snapshot from its own
        construction and never sees the other's spend, so a `max_per_day`
        ceiling of 10 lets two phases spend 8 apiece and neither one stops —
        the file is correct while both in-memory views under-count. Reading
        only the appended tail keeps that cheap even on a long ledger.
        """
        if self.path is None or not self.path.exists():
            return
        with open(self.path, "rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        if not chunk:
            return
        # Stop at the last complete line; a partial tail is left for next time,
        # when the writer will have finished it.
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return
        self._offset += cut + 1
        for line in chunk[: cut + 1].decode("utf-8", "replace").splitlines():
            row = _parse_row(line)
            if row is not None:
                self.rows.append(row)

    def charge(self, row: Charge) -> Charge:
        """Record a charge, durably if this ledger has a path.

        Syncs first so the row lands after anything another ledger appended
        while this one was idle.
        """
        if self.path is not None:
            self._sync()
            _append_row(self.path, row)
            self._offset += len(_encode(row))
        self.rows.append(row)
        return row

    # -- totals ------------------------------------------------------------

    def total(self, *, phase: Phase | None = None, since: datetime | None = None) -> float:
        return sum(r.cost for r in self._select(phase=phase, since=since))

    def attempts(self, *, phase: Phase | None = None) -> int:
        """Inference calls made. Cache hits are not calls."""
        return sum(1 for r in self._select(phase=phase) if not r.cached)

    def wasted(self, *, phase: Phase | None = None) -> float:
        """Spend on generations that were rejected and regenerated.

        Surfaced on its own because it is the number that tells you a validation
        rule is too tight, and it is invisible in a single total.
        """
        return sum(r.cost for r in self._select(phase=phase) if r.failed)

    def by_phase(self) -> dict[Phase, float]:
        out: dict[Phase, float] = {}
        for row in self.rows:
            out[row.phase] = out.get(row.phase, 0.0) + row.cost
        return out

    def by_unit(self, *, phase: Phase | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in self._select(phase=phase):
            out[row.unit] = out.get(row.unit, 0.0) + row.cost
        return out

    def today(self, *, now: datetime | None = None) -> float:
        """Spend since midnight UTC.

        Calendar day rather than a rolling window, so the number a user sees
        matches the one they would compute themselves from a day's invoices.
        """
        current = now or datetime.now(timezone.utc)
        midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.total(since=midnight)

    def estimated_share(self) -> float:
        """Fraction of spend that was estimated rather than reported."""
        total = self.total()
        if not total:
            return 0.0
        return sum(r.cost for r in self.rows if r.cost_is_estimated) / total

    def _select(self, *, phase: Phase | None = None, since: datetime | None = None):
        # Every total goes through here, so this is the one place that has to
        # pick up another process's or another ledger's appends.
        self._sync()
        for row in self.rows:
            if phase is not None and row.phase != phase:
                continue
            if since is not None and _parse_at(row.at) < since:
                continue
            yield row


def _parse_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _encode(row: Charge) -> bytes:
    return (json.dumps(asdict(row), sort_keys=True) + "\n").encode("utf-8")


def _append_row(path: Path, row: Charge) -> None:
    """Append one row and flush it to disk.

    `fsync` on every row looks excessive until a run is killed by a budget stop
    or an OOM and the last few charges are still in the page cache. Losing the
    tail of the ledger is losing exactly the spend that mattered.

    Opened in append mode per row, so two ledgers writing the same file
    interleave whole lines rather than overwriting each other.
    """
    with open(path, "ab") as handle:
        handle.write(_encode(row))
        handle.flush()
        os.fsync(handle.fileno())


def _parse_row(line: str) -> Charge | None:
    """Parse one row, returning None for anything unreadable.

    A partial write from a hard kill must not make the ledger unreadable — that
    would turn a crash into a silent reset of the daily total.
    """
    line = line.strip()
    if not line:
        return None
    try:
        return Charge(**json.loads(line))
    except (json.JSONDecodeError, TypeError):
        return None


def day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The UTC calendar day containing `now`."""
    current = now or datetime.now(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
