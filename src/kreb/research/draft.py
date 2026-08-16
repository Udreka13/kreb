"""What the model is allowed to return, and how it becomes a `Section`.

The central invariant of the whole pipeline lives here: **the model names
symbols, but it never authors an `Anchor`.** A draft carries bare refs as
strings; the pipeline looks each one up in the index and fills in `text_hash`
from what it found. A ref that resolves to nothing produces no anchor at all and
a rejection reason instead.

That is what makes `doc/validate.py`'s moved-versus-misplaced rule sound. It
treats a matching `text_hash` at a different ref as a relocation rather than an
invention, and that reasoning only holds because a model cannot produce a
matching hash — it never sees one, and never writes one. Letting a draft carry
its own `text_hash` would quietly invalidate a decision made two modules away.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from kreb.doc.schema import Anchor, Confidence, Evidence, Section, SectionKind
from kreb.doc.scrub import redact
from kreb.index.repo_index import RepoIndex


class EvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: str = "symbol"
    ref: str
    note: str = ""


class SectionDraft(BaseModel):
    """The model's output for one section, before the pipeline validates it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    body: str
    # Bare refs. Deliberately no hash field — see the module docstring.
    cites: list[str] = Field(default_factory=list)
    confidence: Confidence = "derived"
    evidence: list[EvidenceDraft] = Field(default_factory=list)


class MaterializedSection(BaseModel):
    """A draft that survived resolution, plus what had to be dropped."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    section: Section
    rejections: list[str] = Field(default_factory=list)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_draft(text: str) -> SectionDraft:
    """Parse model output into a draft, tolerating a markdown code fence.

    Nothing more forgiving than that. Repairing malformed JSON by hand is how a
    parser starts silently accepting a shape the schema forbids — and the retry
    loop exists precisely so that a bad generation can be thrown away.
    """
    stripped = _FENCE.sub("", text).strip()
    if not stripped:
        raise ValueError("empty response")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return SectionDraft.model_validate(payload)


def materialize(
    draft: SectionDraft,
    *,
    section_id: str,
    title: str,
    kind: SectionKind,
    index: RepoIndex,
    parent_id: str | None = None,
) -> MaterializedSection:
    """Turn a draft into a `Section`, resolving every citation against the index.

    Rejections are returned rather than raised. The caller decides whether to
    retry, and a section that cited one bad symbol among five is usually worth
    regenerating rather than abandoning — but that judgement is not this
    function's to make.
    """
    rejections: list[str] = []
    anchors: list[Anchor] = []
    seen: set[str] = set()

    for ref in draft.cites:
        if ref in seen:
            continue
        seen.add(ref)
        symbol = index.resolve(ref)
        if symbol is None:
            status = index.anchor_status(ref)
            rejections.append(
                f"cited symbol {ref!r} is {status}; cite a symbol that exists at the path given"
            )
            continue
        anchors.append(
            Anchor(
                ref=symbol.ref,
                # From the index, never from the model.
                text_hash=symbol.text_hash,
                lines=(symbol.start_line, symbol.end_line),
            )
        )

    evidence: list[Evidence] = []
    for item in draft.evidence:
        kind_value = item.kind if item.kind in _EVIDENCE_KINDS else "external"
        evidence.append(
            Evidence(kind=kind_value, ref=item.ref, note=item.note, confidence=draft.confidence)
        )
    for anchor in anchors:
        if not any(e.kind == "symbol" and e.ref == anchor.ref for e in evidence):
            evidence.append(Evidence(kind="symbol", ref=anchor.ref, confidence=draft.confidence))

    # `verified` requires a resolving anchor. Downgrading here rather than
    # letting validation reject it keeps a good section that was merely
    # over-confident, instead of spending three more generations on it.
    confidence = draft.confidence
    if confidence == "verified" and not anchors:
        confidence = "derived"
        rejections.append("claimed `verified` without citing a resolvable symbol; downgraded")

    section = Section(
        id=section_id,
        title=title,
        kind=kind,
        # The model reads repository source, so its output can contain whatever
        # was in that source. Scrubbing here rather than at render time means a
        # credential never reaches the stored artifact either.
        body=redact(draft.body),
        confidence=confidence,
        anchors=tuple(anchors),
        evidence=tuple(evidence),
        parent_id=parent_id,
    )
    return MaterializedSection(section=section, rejections=rejections)


_EVIDENCE_KINDS = frozenset({"symbol", "commit", "pull_request", "issue", "external"})
