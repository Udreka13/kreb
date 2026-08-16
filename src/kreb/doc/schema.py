"""The document schema — the contract every renderer reads and the model writes.

Three decisions are load-bearing here.

**Staleness is computed, never stored.** An `Anchor` records the `text_hash` of
the symbol *as it was when the claim was written*. Whether the claim is still
fresh is a comparison against the current index, made at read time. Storing a
`stale: bool` would mean a document could be wrong about its own freshness,
which is the failure this project exists to avoid, arriving through the back
door.

**Confidence is a property of a claim, not a vibe.** `verified` is only
available to a section carrying a resolving symbol anchor; nothing else in the
schema can produce it. The validator enforces that, so a model cannot type its
way to authority.

**`external` evidence is a distinct kind, not a URL in a note.** The single
most dangerous output kreb can produce is library documentation restated as this
repository's behaviour — well-researched, confident, and wrong at exactly the
point the reader cares about. Making external evidence structurally distinct is
what lets a validator count it separately.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

Confidence = Literal["verified", "derived", "speculative"]

# `structure` describes what the code is; `rationale` describes why it is that
# way; `background` describes the libraries and conventions around it and is
# explicitly *not* about this repository. The distinction drives both validation
# and rendering — a background section is visually contained, because
# containment at render time beats detection.
SectionKind = Literal["overview", "structure", "rationale", "background"]

EvidenceKind = Literal["symbol", "commit", "pull_request", "issue", "external"]

Staleness = Literal["fresh", "stale", "moved", "broken"]

AnchorStatus = Literal["resolved", "misplaced", "ambiguous", "fabricated"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Anchor(Base):
    """A citation into the repository, pinned to the content it described."""

    ref: str = Field(description="`path#Qualified.name`, per index.repo_index.symbol_ref")
    text_hash: str = Field(description="the symbol's text_hash when the claim was written")
    lines: tuple[int, int] | None = None

    @field_validator("ref")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if "#" not in value or value.startswith("#") or value.endswith("#"):
            raise ValueError(f"anchor ref must be 'path#qualname', got {value!r}")
        return value

    @property
    def path(self) -> str:
        return self.ref.partition("#")[0]

    @property
    def qualname(self) -> str:
        return self.ref.partition("#")[2]


class Evidence(Base):
    """Why a claim is believed, with the method that produced it attached."""

    kind: EvidenceKind
    ref: str = Field(description="symbol ref, commit sha, PR/issue number, or URL")
    note: str = ""
    confidence: Confidence = "derived"
    # How this was found. `archaeology/` fills this with e.g. "pickaxe+blame";
    # it is provenance for the claim about the claim.
    method: str = ""

    @property
    def is_external(self) -> bool:
        return self.kind == "external"


class DiagramSpec(Base):
    """A diagram as d2 source plus how it was obtained.

    Produced during document construction, where `index/` access is legal, and
    carried in the document so every renderer consumes it identically — and so
    the `extracted` vs `asserted` distinction travels with it. A diagram drawn
    from the model's belief about the code and one traversed out of the AST look
    identical on screen; only this field separates them.
    """

    title: str
    d2_source: str
    provenance: Literal["extracted", "asserted"]
    anchors: tuple[Anchor, ...] = ()


class Section(Base):
    """The unit of work, of caching, and of invalidation.

    The document is *not* the DAG node — this is. A 40-section document that
    fails at section 39 must not discard 38 good sections, and a commit touching
    one file must not invalidate the other 39.
    """

    id: str
    title: str
    kind: SectionKind
    body: str
    confidence: Confidence = "derived"
    anchors: tuple[Anchor, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    diagram: DiagramSpec | None = None
    # Sections this one was written against, for the stitch pass and for
    # rendering order. Not a tree: a section may be referenced from several.
    parent_id: str | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not value or any(c.isspace() for c in value):
            raise ValueError(f"section id must be a non-empty slug, got {value!r}")
        return value

    @property
    def repo_evidence(self) -> tuple[Evidence, ...]:
        """Evidence that is about *this repository*, not about the world."""
        return tuple(e for e in self.evidence if not e.is_external)


class Capabilities(Base):
    """What was actually available when this document was built.

    Without this, a document built on a shallow clone with no forge access and
    no web looks exactly as authoritative as one built with everything. Every
    renderer surfaces it. It is ~50 lines and it carries the one property that
    distinguishes kreb from a confident summarizer.
    """

    base_sha: str
    git: Literal["ok", "shallow", "unavailable"] = "ok"
    forge: Literal["authenticated", "anonymous", "none", "rate_limited"] = "none"
    web: Literal["off", "deps", "full"] = "off"
    languages: tuple[str, ...] = ()
    degraded_files: int = 0
    total_files: int = 0
    dirty: bool = False
    notes: tuple[str, ...] = ()

    def warnings(self) -> list[str]:
        """Everything a reader must know to discount this document correctly."""
        out: list[str] = []
        if self.git == "shallow":
            out.append("Shallow clone: no history, so no rationale could be recovered.")
        elif self.git == "unavailable":
            out.append("No git history available.")
        if self.forge in ("none", "rate_limited"):
            out.append(
                "No pull-request evidence: "
                + ("forge unreachable." if self.forge == "none" else "rate limit reached.")
            )
        if self.dirty:
            out.append("Working tree was dirty; content was read at the pinned commit.")
        if self.degraded_files:
            share = f" ({self.degraded_files}/{self.total_files})" if self.total_files else ""
            out.append(f"{self.degraded_files} files{share} had no symbol index; "
                       "claims about them are unvalidated.")
        out.extend(self.notes)
        return out


class Document(Base):
    """A research document, and everything needed to judge it."""

    schema_version: int = SCHEMA_VERSION
    title: str
    question: str = ""
    repo_name: str = ""
    sections: tuple[Section, ...] = ()
    capabilities: Capabilities
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("sections")
    @classmethod
    def _unique_ids(cls, value: tuple[Section, ...]) -> tuple[Section, ...]:
        seen: set[str] = set()
        for section in value:
            if section.id in seen:
                raise ValueError(f"duplicate section id: {section.id!r}")
            seen.add(section.id)
        return value

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def anchors(self) -> list[tuple[Section, Anchor]]:
        pairs: list[tuple[Section, Anchor]] = []
        for section in self.sections:
            pairs.extend((section, a) for a in section.anchors)
            if section.diagram:
                pairs.extend((section, a) for a in section.diagram.anchors)
        return pairs

    # -- persistence -------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Document:
        data = json.loads(text)
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"document schema version {version!r} is not {SCHEMA_VERSION}; "
                "regenerate rather than reading it as if it matched"
            )
        return cls.model_validate(data)

    def write(self, path: Path | str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path | str) -> Document:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
