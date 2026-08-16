"""The document schema, its validators, and the Gate A harness."""

from kreb.doc.gate_a import Check, GateAResult, staleness_recall
from kreb.doc.gate_a import run as gate_a
from kreb.doc.schema import (
    SCHEMA_VERSION,
    Anchor,
    Capabilities,
    DiagramSpec,
    Document,
    Evidence,
    Section,
)
from kreb.doc.validate import Finding, Report, anchor_staleness, identifiers_in, validate

__all__ = [
    "SCHEMA_VERSION",
    "Anchor",
    "Capabilities",
    "Check",
    "DiagramSpec",
    "Document",
    "Evidence",
    "Finding",
    "GateAResult",
    "Report",
    "Section",
    "anchor_staleness",
    "gate_a",
    "identifiers_in",
    "staleness_recall",
    "validate",
]
