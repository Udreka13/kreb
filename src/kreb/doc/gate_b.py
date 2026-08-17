"""Gate B — human usefulness. The only gate that decides whether this is worth it.

Gate A proves a document is *factual*. A document can pass all six of its checks
by being trivially, verifiably true and worth nothing. Gate B asks the question
that actually matters: on a repository you know cold, does this tell you ≥3 true
things you did not already know, and 0 things that are wrong while claiming to be
`verified`?

**This module scores nothing.** It builds a worksheet and does arithmetic on
counts a human supplies. That restraint is the whole design:

- *Novelty is not observable from here.* Whether a claim is new depends on what
  the reader already knew, which lives in their head and nowhere in the artifact.
- *Truth at `verified` is the thing under test.* Asking the pipeline to grade its
  own output on the one axis it is being tested for is not a gate, it is a
  mirror. If a model could tell which of its claims were wrong, it would not have
  written them.

So what is automatable here is not judgement but **the cost of judging**. The
expensive part of checking a claim is not deciding — it is navigating: opening the
file, finding the lines, holding the claim in your head while you read. The
worksheet puts each claim next to the source its anchors point at, so the reader
decides and never navigates.

One deliberate omission: claims are split on sentence boundaries, which is
approximate. A sentence is a decent proxy for "a statement" and a bad one for
some prose. The reader is the unit of judgement; the split is scaffolding, and
`skipped` is a legitimate mark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kreb.doc.schema import Anchor, Confidence, Document
from kreb.doc.validate import Report, all_anchors, anchor_staleness, validate
from kreb.index.repo_index import RepoIndex
from kreb.repo.access import Repository

# The thresholds, as data rather than as prose, so they appear in every sheet
# and cannot drift to wherever the current output happens to land.
NOVEL_TRUE_REQUIRED = 3
WRONG_AT_VERIFIED_ALLOWED = 0

# Sentence-ish. Splits after `.`, `?` or `!` followed by whitespace and a capital
# or a backtick, which keeps `path/to/file.py` and `v2.13` intact. Inline code
# spans are masked first so a period inside one never splits.
_SENTENCE = re.compile(r"(?<=[.?!])\s+(?=[A-Z`])")
_CODE_SPAN = re.compile(r"`[^`]*`")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)


@dataclass(frozen=True)
class AnchorView:
    """An anchor with the code it points at already fetched."""

    ref: str
    path: str
    lines: tuple[int, int] | None
    staleness: str
    source: str = ""
    unavailable: str = ""

    @property
    def location(self) -> str:
        if self.lines:
            return f"{self.path}:{self.lines[0]}-{self.lines[1]}"
        return self.path


@dataclass(frozen=True)
class Claim:
    """One statement to be judged, with everything needed to judge it."""

    section_id: str
    section_title: str
    confidence: Confidence
    kind: str
    text: str
    anchors: tuple[AnchorView, ...] = ()

    @property
    def at_verified(self) -> bool:
        """Only these can fail the zero-wrong threshold.

        A wrong `speculative` claim is a document doing its job — it hedged. The
        gate is about claims that assert certainty and do not have it.
        """
        return self.confidence == "verified"


@dataclass
class Verdict:
    novel_true: int
    wrong_at_verified: int
    checked: int = 0

    @property
    def passed(self) -> bool:
        return (
            self.novel_true >= NOVEL_TRUE_REQUIRED
            and self.wrong_at_verified <= WRONG_AT_VERIFIED_ALLOWED
        )

    def summary(self) -> str:
        novelty = "met" if self.novel_true >= NOVEL_TRUE_REQUIRED else "NOT met"
        honesty = "met" if self.wrong_at_verified <= WRONG_AT_VERIFIED_ALLOWED else "NOT met"
        return "\n".join(
            [
                f"novel and true: {self.novel_true} "
                f"(need ≥{NOVEL_TRUE_REQUIRED}) — {novelty}",
                f"wrong at `verified`: {self.wrong_at_verified} "
                f"(need {WRONG_AT_VERIFIED_ALLOWED}) — {honesty}",
                "",
                f"Gate B: {'PASS' if self.passed else 'FAIL'}",
            ]
        )


@dataclass
class Worksheet:
    """Everything a reader needs, and no judgement they have not made."""

    title: str
    question: str
    base_sha: str
    repo_name: str = ""
    claims: list[Claim] = field(default_factory=list)
    caveats: tuple[str, ...] = ()
    report: Report | None = None

    @property
    def verified_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.at_verified]

    def score(self, *, novel_true: int, wrong_at_verified: int) -> Verdict:
        """Apply the thresholds to counts a human supplied."""
        return Verdict(
            novel_true=novel_true,
            wrong_at_verified=wrong_at_verified,
            checked=len(self.claims),
        )


def build(
    document: Document,
    index: RepoIndex,
    repo: Repository | None = None,
    *,
    report: Report | None = None,
) -> Worksheet:
    """Turn a document into a sheet of judgeable claims."""
    checked = report if report is not None else validate(document, index)
    sheet = Worksheet(
        title=document.title,
        question=document.question,
        base_sha=document.capabilities.base_sha,
        caveats=tuple(document.capabilities.warnings()),
        report=checked,
    )

    for section in document.sections:
        # Background sections describe the library, not the repository. They are
        # not what Gate B is measuring and counting them would inflate novelty
        # with facts about somebody else's code.
        if section.kind == "background":
            continue
        views = tuple(_view(a, index, repo) for a in all_anchors(section))
        for text in split_claims(section.body):
            sheet.claims.append(
                Claim(
                    section_id=section.id,
                    section_title=section.title,
                    confidence=section.confidence,
                    kind=section.kind,
                    text=text,
                    anchors=views,
                )
            )
    return sheet


def split_claims(body: str) -> list[str]:
    """Split prose into statements, treating each bullet as its own.

    Bullets are where a section makes its most checkable assertions, and joining
    them into one blob would force a reader to mark a list true or false as a
    unit — which is how a wrong item hides behind four right ones.
    """
    statements: list[str] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        if any(_BULLET.match(line) for line in lines):
            for line in lines:
                item = _BULLET.sub("", line).strip()
                if item:
                    statements.append(item)
            continue
        for sentence in _split_sentences(" ".join(lines)):
            if sentence:
                statements.append(sentence)
    return statements


def _split_sentences(text: str) -> list[str]:
    """Split on sentence ends outside inline code spans."""
    # Mask code spans to a same-length run of `x`, so offsets stay valid and no
    # period inside `os.path.join` can end a sentence.
    masked = _CODE_SPAN.sub(lambda m: "x" * len(m.group()), text)
    out, start = [], 0
    for match in _SENTENCE.finditer(masked):
        out.append(text[start : match.start()].strip())
        start = match.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def _view(anchor: Anchor, index: RepoIndex, repo: Repository | None) -> AnchorView:
    state, _ = anchor_staleness(anchor, index)
    source, unavailable = "", ""
    symbol = index.resolve(anchor.ref)
    lines = anchor.lines or (
        (symbol.start_line, symbol.end_line) if symbol is not None else None
    )

    if repo is None:
        unavailable = "no repository given; source not shown"
    elif symbol is None:
        unavailable = "this symbol is not in the index at this commit"
    elif lines is None:
        unavailable = "the anchor carries no line range"
    else:
        try:
            body = repo.read(symbol.path).decode("utf-8", "replace").split("\n")
            source = "\n".join(body[lines[0] - 1 : lines[1]])
        except Exception as exc:  # pragma: no cover - defensive
            unavailable = f"could not read {symbol.path}: {exc}"

    return AnchorView(
        ref=anchor.ref,
        path=anchor.path,
        lines=lines,
        staleness=state,
        source=source,
        unavailable=unavailable,
    )
