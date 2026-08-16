"""Document → a single self-contained HTML file.

**Zero JavaScript**, and that is a product decision rather than minimalism. The
output is something people commit to a repository, open from a file:// path,
and read on machines they do not control. A script tag turns a document into
something that has to be trusted; without one it is a document.

Everything is inlined — CSS, no external fonts, no CDN — so the file works from
a USB stick, a `file://` URL, or an artifact attached to a CI run.

Like the markdown renderer, this adds no judgement. The one thing it does that
markdown cannot is *contain* things visually: background sections are boxed and
labelled, confidence is a coloured chip rather than an italic word, and stale
anchors are struck through. Containment at render time is the mitigation the
architecture chose over trying to detect repo-scoped claims semantically.
"""

from __future__ import annotations

import html
import re

from kreb.doc.schema import Document, Section
from kreb.doc.validate import Report

_STYLE = """\
:root {
  --bg: #fdfdfc; --fg: #1a1a19; --muted: #6b6b66; --rule: #e2e1dc;
  --card: #f6f5f2; --accent: #8a5a2b;
  --ok: #2f6b45; --warn: #8a6d1f; --spec: #7a5c8a; --bad: #99342b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e6e5e1; --muted: #9a988f; --rule: #2e3034;
    --card: #1e2024; --accent: #d99f63;
    --ok: #7fb894; --warn: #d4b45e; --spec: #b79ac7; --bad: #e08b80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.25rem 5rem; background: var(--bg); color: var(--fg);
  font: 16px/1.65 ui-serif, Georgia, "Times New Roman", serif;
}
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 .5rem; letter-spacing: -.01em; }
h2 { font-size: 1.35rem; margin: 2.75rem 0 .35rem; letter-spacing: -.005em; }
h3 { font-size: 1rem; margin: 1.5rem 0 .35rem; }
p { margin: 0 0 1rem; }
a { color: var(--accent); }
code, pre {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: .87em;
}
code { background: var(--card); padding: .1em .35em; border-radius: 3px; }
pre {
  background: var(--card); padding: .9rem 1rem; border-radius: 6px;
  overflow-x: auto; border: 1px solid var(--rule);
}
pre code { background: none; padding: 0; }
.question { color: var(--muted); font-style: italic; margin-bottom: 2rem; }
.manifest {
  background: var(--card); border: 1px solid var(--rule); border-radius: 6px;
  padding: 1rem 1.25rem; margin: 1.5rem 0 2.5rem; font-size: .9rem;
}
.manifest h2 { margin: 0 0 .5rem; font-size: .95rem; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); }
.manifest ul { margin: .35rem 0 0; padding-left: 1.1rem; }
.limits { margin-top: .85rem; padding-top: .75rem; border-top: 1px solid var(--rule); }
.limits strong { color: var(--warn); }
.chip {
  display: inline-block; font-family: ui-monospace, monospace; font-size: .72rem;
  text-transform: uppercase; letter-spacing: .05em; padding: .12rem .5rem;
  border-radius: 999px; border: 1px solid currentColor; vertical-align: .15em;
}
.chip.verified { color: var(--ok); }
.chip.derived { color: var(--warn); }
.chip.speculative { color: var(--spec); }
.background {
  border-left: 3px solid var(--spec); padding-left: 1.1rem; margin-left: -1.25rem;
}
.background-label {
  color: var(--spec); font-size: .8rem; text-transform: uppercase;
  letter-spacing: .06em; margin-bottom: .5rem;
}
.evidence { font-size: .88rem; margin-top: 1.25rem; }
.evidence h3 {
  font-size: .78rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 1rem 0 .3rem;
}
.evidence ul { margin: 0; padding-left: 1.1rem; }
.evidence li { margin: .15rem 0; }
.stale { color: var(--warn); }
.broken { color: var(--bad); text-decoration: line-through; }
.moved { color: var(--muted); }
.provenance { color: var(--muted); font-size: .85rem; font-style: italic; }
footer {
  margin-top: 4rem; padding-top: 1.25rem; border-top: 1px solid var(--rule);
  color: var(--muted); font-size: .85rem;
}
"""

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)


def render(doc: Document, report: Report | None = None) -> str:
    """Render a complete, self-contained HTML document."""
    body = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(doc.title)}</title>",
        f"<style>{_STYLE}</style>",
        "</head><body><main>",
        f"<h1>{html.escape(doc.title)}</h1>",
    ]
    if doc.question:
        body.append(f'<p class="question">{html.escape(doc.question)}</p>')

    body.append(_manifest(doc))
    for section in doc.sections:
        body.append(_section(section, report))
    body.append(_footer(doc))
    body.append("</main></body></html>")
    return "\n".join(body)


def _manifest(doc: Document) -> str:
    caps = doc.capabilities
    items = [
        f"commit <code>{html.escape(caps.base_sha[:12])}</code>",
        f"languages: {html.escape(', '.join(caps.languages) or 'none')}",
        f"git: {caps.git} · forge: {caps.forge} · web: {caps.web}",
    ]
    listed = "".join(f"<li>{i}</li>" for i in items)
    out = [
        '<section class="manifest"><h2>What this is built on</h2>',
        f"<ul>{listed}</ul>",
    ]
    warnings = caps.warnings()
    if warnings:
        warned = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        out.append(
            f'<div class="limits"><strong>Read with these limits in mind</strong>'
            f"<ul>{warned}</ul></div>"
        )
    out.append("</section>")
    return "".join(out)


def _section(section: Section, report: Report | None) -> str:
    classes = "background" if section.kind == "background" else ""
    out = [f'<section class="{classes}">' if classes else "<section>"]
    if section.kind == "background":
        out.append(
            '<div class="background-label">About the library, not this repository</div>'
        )
    chip = f'<span class="chip {section.confidence}">{section.confidence}</span>'
    out.append(f"<h2>{html.escape(section.title)} {chip}</h2>")
    out.append(_markdown_to_html(section.body))

    if section.diagram:
        diagram = section.diagram
        origin = (
            "extracted from the code"
            if diagram.provenance == "extracted"
            else "drawn from the description, not read from the code"
        )
        out.append(
            f'<p class="provenance">{html.escape(diagram.title)} — {origin}</p>'
            f"<pre><code>{html.escape(diagram.d2_source.strip())}</code></pre>"
        )

    out.append(_evidence(section, report))
    out.append("</section>")
    return "".join(out)


def _evidence(section: Section, report: Report | None) -> str:
    blocks: list[str] = []

    if section.anchors:
        items = []
        for anchor in section.anchors:
            state = report.staleness.get(anchor.ref, "fresh") if report else "fresh"
            cls = {"stale": "stale", "broken": "broken", "moved": "moved"}.get(state, "")
            lines = f" (lines {anchor.lines[0]}–{anchor.lines[1]})" if anchor.lines else ""
            note = ""
            if state == "stale":
                note = ' <span class="stale">— changed since this was written</span>'
            elif state == "broken":
                note = ' <span class="broken">— no longer present</span>'
            elif state == "moved" and report:
                destination = html.escape(report.moved.get(anchor.ref, "?"))
                note = f' <span class="moved">— now at <code>{destination}</code></span>'
            items.append(
                f'<li class="{cls}"><code>{html.escape(anchor.ref)}</code>{lines}{note}</li>'
            )
        blocks.append("<h3>Cited symbols</h3><ul>" + "".join(items) + "</ul>")

    history = [e for e in section.evidence if e.kind in ("commit", "pull_request", "issue")]
    if history:
        items = []
        for item in history:
            label = {"commit": "commit", "pull_request": "PR", "issue": "issue"}[item.kind]
            note = f" — {html.escape(item.note)}" if item.note else ""
            method = f" <em>(via {html.escape(item.method)})</em>" if item.method else ""
            items.append(f"<li>{label} <code>{html.escape(item.ref)}</code>{note}{method}</li>")
        blocks.append("<h3>History</h3><ul>" + "".join(items) + "</ul>")

    external = [e for e in section.evidence if e.is_external]
    if external:
        items = [
            f"<li>{_maybe_link(e.ref)}" + (f" — {html.escape(e.note)}" if e.note else "") + "</li>"
            for e in external
        ]
        blocks.append("<h3>External sources</h3><ul>" + "".join(items) + "</ul>")

    return f'<div class="evidence">{"".join(blocks)}</div>' if blocks else ""


def _maybe_link(ref: str) -> str:
    escaped = html.escape(ref)
    if ref.startswith(("http://", "https://")):
        return f'<a href="{escaped}" rel="noreferrer noopener">{escaped}</a>'
    return escaped


def _footer(doc: Document) -> str:
    when = html.escape(doc.generated_at[:19].replace("T", " "))
    return (
        f"<footer>Generated by kreb at commit "
        f"<code>{html.escape(doc.capabilities.base_sha[:12])}</code> on {when}Z."
        f"<br><strong>verified</strong> follows directly from cited code · "
        f"<strong>derived</strong> is inferred from it · "
        f"<strong>speculative</strong> is unsupported by the evidence gathered.</footer>"
    )


def _markdown_to_html(text: str) -> str:
    """A deliberately small markdown subset.

    Paragraphs, fenced code, inline code, bold, and list items — which is what
    the section prompt asks for. Pulling in a full markdown library would mean
    accepting raw HTML from model output into a file the user opens in a
    browser; everything here escapes first and formats second.
    """
    blocks: list[str] = []
    position = 0

    for match in _FENCE.finditer(text):
        blocks.extend(_prose(text[position : match.start()]))
        language = html.escape(match.group(1) or "")
        code = html.escape(match.group(2))
        attr = f' class="language-{language}"' if language else ""
        blocks.append(f"<pre><code{attr}>{code}</code></pre>")
        position = match.end()

    blocks.extend(_prose(text[position:]))
    return "".join(blocks)


def _prose(text: str) -> list[str]:
    out: list[str] = []
    for chunk in re.split(r"\n\s*\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        if all(line.lstrip().startswith(("- ", "* ")) for line in lines):
            items = "".join(f"<li>{_inline(line.lstrip()[2:])}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{_inline(chunk)}</p>")
    return out


def _inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return escaped.replace("\n", " ")
