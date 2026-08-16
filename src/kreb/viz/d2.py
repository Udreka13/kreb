"""Diagram specs, and rendering them with `d2` when it is available.

`viz/` exists as its own module to fix a real layering violation: extracted
diagrams come from AST traversal, but both the HTML and the video renderer need
them. Producing them inside either renderer would force one to import the other,
or force something under `render/` to import `index/`. So diagram *specs* are
built during document construction, where `index/` access is legal, and every
renderer consumes them like any other section content.

The important half is `provenance`. An **extracted** diagram was traversed out
of the code and carries anchors proving it. An **asserted** diagram is the
model's belief about the structure. On screen they are indistinguishable, so
that distinction has to travel in the data and be printed next to the picture.

`d2` is an optional binary. When it is missing, the spec still renders as its
own source — which is legible, diffable, and honest — rather than the pipeline
failing or the diagram silently vanishing.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from kreb.doc.schema import Anchor, DiagramSpec
from kreb.index.repo_index import RepoIndex

D2_TIMEOUT = 30.0


@dataclass
class RenderedDiagram:
    """An SVG, or the reason there isn't one."""

    svg: str | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.svg is not None


def d2_available() -> bool:
    return shutil.which("d2") is not None


def render_svg(spec: DiagramSpec, *, theme: str = "0") -> RenderedDiagram:
    """Render a spec to SVG, degrading to a stated reason if `d2` is absent."""
    if not d2_available():
        return RenderedDiagram(
            svg=None,
            reason="d2 is not installed; the diagram is shown as its source instead",
        )
    try:
        proc = subprocess.run(
            ["d2", "--theme", theme, "-", "-"],
            input=spec.d2_source.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=D2_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RenderedDiagram(svg=None, reason=f"d2 failed: {exc}")

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        return RenderedDiagram(svg=None, reason=f"d2 rejected the diagram: {detail}")
    return RenderedDiagram(svg=proc.stdout.decode("utf-8", "replace"))


def _label(path: str) -> str:
    """A d2-safe identifier for a file path."""
    return path.replace("/", "_").replace(".", "_").replace("-", "_")


def import_diagram(
    index: RepoIndex, paths: list[str], *, title: str = "Module dependencies"
) -> DiagramSpec | None:
    """Build an **extracted** diagram of imports between the given files.

    Read from the import graph rather than described, so it carries anchors and
    may legitimately claim `extracted`. Returns None when there are no edges —
    an empty diagram asserts "these modules do not depend on each other", which
    is a claim, and usually a false one made by an incomplete index.
    """
    wanted = [p for p in paths if p in index.files]
    if len(wanted) < 2:
        return None

    edges: list[tuple[str, str]] = []
    for path in wanted:
        entry = index.files[path]
        for target in entry.imports:
            if target in wanted and target != path:
                edges.append((path, target))

    if not edges:
        return None

    lines = [f"# {title}", "direction: right"]
    for path in wanted:
        lines.append(f'{_label(path)}: "{path}"')
    for source, target in sorted(set(edges)):
        lines.append(f"{_label(source)} -> {_label(target)}")

    anchors: list[Anchor] = []
    for path in wanted:
        symbol = next(
            (s for s in index.symbols.values() if s.path == path),
            None,
        )
        if symbol is not None:
            anchors.append(Anchor(ref=symbol.ref, text_hash=symbol.text_hash))

    return DiagramSpec(
        title=title,
        d2_source="\n".join(lines),
        provenance="extracted",
        anchors=tuple(anchors),
    )


def asserted_diagram(title: str, d2_source: str) -> DiagramSpec:
    """Wrap model-authored diagram source, labelled as the assertion it is."""
    return DiagramSpec(title=title, d2_source=d2_source, provenance="asserted")
