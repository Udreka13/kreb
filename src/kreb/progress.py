"""Progress reporting — a typed event stream with interchangeable sinks.

A research pass runs for minutes and spends real money, and the first version
did both in complete silence. That is not merely unfriendly: with no output
there is no way to tell a working run from a hung one, from one burning the
ceiling on retries, without reading `.kreb/spend.jsonl` by hand.

Two decisions make this reusable rather than a pile of prints.

**Everything goes to stderr.** stdout carries the contract — artifact paths and
`--json` payloads — and an adapter piping stdout into a parser must not receive
progress chatter. Splitting the streams is what lets a human watch a run whose
output is being consumed by a machine.

**Events are structured, and rendering is a sink's job.** The CLI wants lines on
a terminal; MCP will want `notifications/progress` with a progress token; a CI
job wants JSON Lines. All three consume the same events, so the engine emits
facts and never formats. Each event carries `seq`, `done` and `total`, which is
exactly what a progress notification needs.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, TextIO


@dataclass
class Event:
    """One thing that happened, in a shape any sink can render."""

    kind: str
    message: str = ""
    # Position in the run, for progress bars and MCP progress tokens.
    done: int = 0
    total: int = 0
    seq: int = 0
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def fraction(self) -> float | None:
        return (self.done / self.total) if self.total else None


class Reporter(Protocol):
    def emit(self, event: Event) -> None: ...


class NullReporter:
    """Reports nothing. The default, so library use stays silent."""

    def emit(self, event: Event) -> None:  # pragma: no cover - trivial
        return None


class Recorder:
    """Keeps events in memory. For tests, and for a run summary after the fact."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


class JsonlReporter:
    """One JSON object per line. For adapters, CI, and later MCP."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr

    def emit(self, event: Event) -> None:
        self.stream.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        self.stream.flush()


class ConsoleReporter:
    """Human-readable lines on stderr.

    On a terminal, an in-progress section is redrawn in place so the display
    stays one line per section rather than scrolling twice. When stderr is a
    pipe or a file, that would produce carriage-return litter in the log, so it
    falls back to printing only completions — which is what a log wants anyway.
    """

    def __init__(self, stream: TextIO | None = None, *, tty: bool | None = None) -> None:
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty() if tty is None else tty
        self.started = time.time()
        self.spent = 0.0
        self._pending = ""

    # -- drawing -----------------------------------------------------------

    def _clear(self) -> None:
        if self.tty and self._pending:
            self.stream.write("\r" + " " * (len(self._pending) + 2) + "\r")
        self._pending = ""

    def _line(self, text: str) -> None:
        self._clear()
        self.stream.write(text + "\n")
        self.stream.flush()

    def _transient(self, text: str) -> None:
        """A line that will be replaced by its own completion."""
        if not self.tty:
            return
        self._clear()
        self.stream.write(text)
        self.stream.flush()
        self._pending = text

    # -- rendering ---------------------------------------------------------

    def emit(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)

    def _on_run_started(self, event: Event) -> None:
        self.started = time.time()
        data = event.data
        ceiling = data.get("ceiling")
        limit = f"ceiling ${ceiling:.2f}" if ceiling else "no ceiling"
        self._line(
            f"kreb · {event.total} sections · {data.get('model', '?')} · {limit}"
        )

    def _on_section_started(self, event: Event) -> None:
        self._transient(f"  [{event.done}/{event.total}] {event.data.get('id', '')} …")

    def _on_archaeology(self, event: Event) -> None:
        # The pickaxe is the slowest thing in the pipeline and costs nothing, so
        # a stall here looks identical to a hang unless it is announced.
        self._transient(
            f"  [{event.done}/{event.total}] {event.data.get('id', '')} … searching history"
        )

    def _on_attempt(self, event: Event) -> None:
        if event.data.get("failed"):
            reason = (event.data.get("reason") or "rejected").split(";")[0]
            self._line(
                f"  [{event.done}/{event.total}] {_fit(event.data.get('id', ''))} "
                f"attempt {event.data.get('attempt', 1)} rejected — {reason[:70]}"
            )

    def _on_section_done(self, event: Event) -> None:
        data = event.data
        status = data.get("status", "?")
        attempts = data.get("attempts", 1)
        tries = "" if attempts <= 1 else f" ({attempts} attempts)"
        # Accumulated from the ledger delta the section carries, never from the
        # per-attempt usage: a retried section is billed twice, and summing the
        # attempts as well would report the retry cost twice over.
        self.spent += data.get("cost", 0.0)
        money = "cached" if status == "reused" else f"${data.get('cost', 0.0):.5f}"
        self._line(
            f"  [{event.done}/{event.total}] {_fit(data.get('id', ''))} "
            f"{status:<8}{tries} {money:>10}  {data.get('elapsed', 0.0):.1f}s"
            f"   (${self.spent:.5f} total)"
        )

    def _on_budget_warning(self, event: Event) -> None:
        self._line(f"  ! {event.message}")

    def _on_run_stopped(self, event: Event) -> None:
        self._line(f"  ! stopped: {event.message}")

    def _on_run_finished(self, event: Event) -> None:
        data = event.data
        elapsed = time.time() - self.started
        parts = [f"{data.get('written', 0)} written"]
        if data.get("reused"):
            parts.append(f"{data['reused']} reused")
        if data.get("failed"):
            parts.append(f"{data['failed']} failed")
        if data.get("skipped"):
            parts.append(f"{data['skipped']} not attempted")
        self._line(
            f"done · {', '.join(parts)} · ${data.get('cost', 0.0):.5f} · {elapsed:.0f}s"
        )


def _fit(text: str, width: int = 34) -> str:
    """Pad or elide to a fixed width, so the columns hold.

    Section ids are dotted symbol paths and some are long. Left-truncating keeps
    the distinguishing tail — `…loop.run_research` identifies the section, while
    `research.loop.run…` looks like every other section in the same module.
    """
    if len(text) > width:
        return "…" + text[-(width - 1) :]
    return text.ljust(width)


class Progress:
    """Convenience wrapper that stamps sequence numbers and run position."""

    def __init__(self, reporter: Reporter | None = None, *, total: int = 0) -> None:
        self.reporter = reporter or NullReporter()
        self.total = total
        self.done = 0
        self._seq = 0

    # Positional-only, because the payload is `**data` and an event about a
    # section carries its own `kind`. Without the `/` that collides with the
    # event's own kind and raises, which is a trap set for every future caller.
    def emit(self, kind: str, message: str = "", /, **data: Any) -> None:
        self._seq += 1
        self.reporter.emit(
            Event(
                kind=kind,
                message=message,
                done=self.done,
                total=self.total,
                seq=self._seq,
                data=data,
            )
        )

    def advance(self) -> None:
        self.done += 1


def reporter_for(mode: str, stream: TextIO | None = None) -> Reporter:
    """Pick a sink by name. `auto` means human on a terminal, quiet otherwise.

    Quiet-when-piped matters: a run whose stdout feeds a parser is usually
    running unattended, and progress lines then just fill a log with text nobody
    reads. `plain` forces them on for the case where somebody does.
    """
    target = stream or sys.stderr
    if mode == "none":
        return NullReporter()
    if mode == "json":
        return JsonlReporter(target)
    if mode == "plain":
        return ConsoleReporter(target, tty=False)
    if mode == "auto":
        return ConsoleReporter(target) if target.isatty() else NullReporter()
    return ConsoleReporter(target)
