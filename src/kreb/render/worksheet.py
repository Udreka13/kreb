"""The Gate B worksheet, rendered for reading rather than for a terminal.

This is a working surface, not a report. Its whole job is to make one judgement
cheap enough to repeat sixty times: *is this claim true, and did I already know
it?* Everything here follows from that.

**The source sits under the claim, not behind a link.** A link is a decision to
open a file, and a decision repeated sixty times is a gate nobody finishes.

**Claims that assert certainty are marked as such.** Only `verified` claims can
fail the zero-wrong threshold, so the reader needs to see at a glance which ones
carry that weight — the rest can be skimmed.

**Nothing is pre-scored.** The marks are empty boxes. A sheet that arrived with
a suggested verdict would anchor the reader to the pipeline's own opinion, which
is the thing under test.

Zero JavaScript, like the document renderer: this is output people commit and
open from `file://` paths.
"""

from __future__ import annotations

import html

from kreb.doc.gate_b import (
    NOVEL_TRUE_REQUIRED,
    WRONG_AT_VERIFIED_ALLOWED,
    Claim,
    Worksheet,
)

_CSS = """
:root {
  --ink: #1a1a1a; --dim: #6b6b6b; --line: #e0ddd6; --bg: #fbfaf7;
  --card: #ffffff; --code-bg: #f4f2ed;
  --sure: #8a4b1f; --sure-bg: #fdf1e6;
  --hedge: #4a5a6a; --hedge-bg: #eef1f4;
  --warn: #8a1f1f; --warn-bg: #fdecec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8e6e1; --dim: #9a968e; --line: #35322c; --bg: #16150f;
    --card: #1e1c17; --code-bg: #14130e;
    --sure: #e0a065; --sure-bg: #2a1d0f;
    --hedge: #9db4c8; --hedge-bg: #171d23;
    --warn: #e08585; --warn-bg: #2a1414;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.25rem 5rem; background: var(--bg); color: var(--ink);
  font: 16px/1.65 ui-serif, Georgia, "Times New Roman", serif;
}
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.65rem; line-height: 1.25; margin: 0 0 .35rem; }
h2 { font-size: 1.1rem; margin: 2.75rem 0 .9rem; padding-bottom: .4rem;
     border-bottom: 1px solid var(--line); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
code { background: var(--code-bg); padding: .1em .3em; border-radius: 3px; font-size: .88em; }
pre { background: var(--code-bg); padding: .7rem .85rem; border-radius: 5px;
      overflow-x: auto; font-size: .8rem; line-height: 1.5; margin: .5rem 0 0; }
pre code { background: none; padding: 0; font-size: inherit; }
.lede { color: var(--dim); margin: 0 0 1.5rem; }
.rules { background: var(--card); border: 1px solid var(--line); border-radius: 6px;
         padding: 1rem 1.25rem; margin: 0 0 2rem; }
.rules p { margin: .4rem 0; }
.rules strong { font-variant-numeric: tabular-nums; }
.claim { background: var(--card); border: 1px solid var(--line); border-left-width: 3px;
         border-radius: 5px; padding: .85rem 1rem; margin: 0 0 .85rem; }
.claim.sure { border-left-color: var(--sure); }
.claim.hedged { border-left-color: var(--hedge); }
.claim > .text { margin: 0 0 .55rem; }
.meta { font-size: .74rem; color: var(--dim); display: flex; flex-wrap: wrap;
        gap: .5rem 1rem; align-items: baseline;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.tag { font-size: .68rem; letter-spacing: .06em; text-transform: uppercase;
       padding: .1em .45em; border-radius: 3px; font-family: inherit; }
.tag.sure { color: var(--sure); background: var(--sure-bg); }
.tag.hedged { color: var(--hedge); background: var(--hedge-bg); }
.tag.stale { color: var(--warn); background: var(--warn-bg); }
.marks { margin: .6rem 0 0; font-size: .8rem; color: var(--dim);
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.marks span { margin-right: 1.1rem; white-space: nowrap; }
details { margin: .5rem 0 0; }
summary { cursor: pointer; font-size: .78rem; color: var(--dim);
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.unavailable { font-size: .78rem; color: var(--warn); margin: .4rem 0 0; }
.caveats { border-left: 3px solid var(--warn); background: var(--warn-bg);
           padding: .7rem 1rem; border-radius: 0 5px 5px 0; margin: 0 0 2rem; }
.caveats ul { margin: .3rem 0 0; padding-left: 1.1rem; }
.tally { background: var(--card); border: 1px solid var(--line); border-radius: 6px;
         padding: 1rem 1.25rem; margin: 2rem 0 0; }
.tally td { padding: .3rem .9rem .3rem 0; font-variant-numeric: tabular-nums; }
.tally td:last-child { border-bottom: 1px solid var(--line); min-width: 4rem; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line);
         font-size: .78rem; color: var(--dim); }
"""


def render(sheet: Worksheet) -> str:
    """One self-contained page holding every claim and the code it cites."""
    verified = sheet.verified_claims
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Gate B — {html.escape(sheet.title)}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        "<h1>Gate B worksheet</h1>",
        f'<p class="lede">{html.escape(sheet.title)}'
        + (f" · asked: <em>{html.escape(sheet.question)}</em>" if sheet.question else "")
        + f" · commit <code>{html.escape(sheet.base_sha[:12])}</code></p>",
        _rules(sheet, verified),
    ]
    if sheet.caveats:
        items = "".join(f"<li>{html.escape(c)}</li>" for c in sheet.caveats)
        out.append(
            "<div class='caveats'><strong>What this run could not see</strong>"
            f"<ul>{items}</ul></div>"
        )

    last = None
    for claim in sheet.claims:
        if claim.section_id != last:
            out.append(f"<h2>{html.escape(claim.section_title)}</h2>")
            last = claim.section_id
        out.append(_claim(claim))

    out.append(_tally(sheet, verified))
    out.append(
        "<footer>Generated by kreb. Nothing on this page has been scored — "
        "the marks are yours.</footer>"
    )
    out.append("</main></body></html>")
    return "\n".join(out)


def _rules(sheet: Worksheet, verified: list[Claim]) -> str:
    return (
        "<div class='rules'>"
        f"<p><strong>{NOVEL_TRUE_REQUIRED}+</strong> claims that are true "
        "<em>and</em> that you did not already know.</p>"
        f"<p><strong>{WRONG_AT_VERIFIED_ALLOWED}</strong> claims that are wrong "
        "while marked <span class='tag sure'>verified</span>. "
        "A wrong hedged claim is the document working.</p>"
        f"<p style='color:var(--dim);margin-top:.8rem'>{len(sheet.claims)} claims, "
        f"{len(verified)} of them at <code>verified</code>. You do not have to mark "
        "every one — read until the novelty bar is met, then check the "
        "<code>verified</code> claims for anything wrong.</p>"
        "</div>"
    )


def _claim(claim: Claim) -> str:
    sure = claim.at_verified
    tag = "sure" if sure else "hedged"
    parts = [
        f"<div class='claim {tag}'>",
        f"<p class='text'>{html.escape(claim.text)}</p>",
        f"<div class='meta'><span class='tag {tag}'>{html.escape(claim.confidence)}</span>"
        f"<span>{html.escape(claim.kind)}</span>",
    ]
    for view in claim.anchors:
        stale = (
            f" <span class='tag stale'>{html.escape(view.staleness)}</span>"
            if view.staleness != "fresh"
            else ""
        )
        parts.append(f"<span><code>{html.escape(view.location)}</code>{stale}</span>")
    parts.append("</div>")

    for view in claim.anchors:
        if view.source:
            parts.append(
                f"<details><summary>{html.escape(view.location)} — "
                f"{html.escape(view.ref.partition('#')[2])}</summary>"
                f"<pre><code>{html.escape(view.source)}</code></pre></details>"
            )
        elif view.unavailable:
            parts.append(f"<p class='unavailable'>{html.escape(view.unavailable)}</p>")

    parts.append(
        "<p class='marks'>"
        "<span>☐ knew it</span><span>☐ novel + true</span>"
        + ("<span>☐ <strong>wrong</strong></span>" if sure else "<span>☐ wrong (hedged)</span>")
        + "<span>☐ skip</span></p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _tally(sheet: Worksheet, verified: list[Claim]) -> str:
    return (
        "<div class='tally'><h2 style='margin-top:0;border:0;padding:0'>Tally</h2>"
        "<table>"
        f"<tr><td>novel + true</td><td></td><td style='color:var(--dim)'>"
        f"need ≥{NOVEL_TRUE_REQUIRED}</td></tr>"
        f"<tr><td>wrong at <code>verified</code></td><td></td>"
        f"<td style='color:var(--dim)'>need {WRONG_AT_VERIFIED_ALLOWED}</td></tr>"
        f"<tr><td>already knew</td><td></td><td style='color:var(--dim)'>"
        f"of {len(sheet.claims)}</td></tr>"
        "</table>"
        "<p style='margin:.9rem 0 0;font-size:.85rem'>Gate B passes only if both "
        "thresholds are met. If it fails, the useful output is <em>which</em> claims "
        "were wrong — that is the next detector.</p>"
        "</div>"
    )


def render_markdown(sheet: Worksheet) -> str:
    """The same sheet as text, for a terminal or a commit."""
    lines = [
        f"# Gate B worksheet — {sheet.title}",
        "",
        f"commit `{sheet.base_sha[:12]}`"
        + (f" · asked: _{sheet.question}_" if sheet.question else ""),
        "",
        f"- **{NOVEL_TRUE_REQUIRED}+** claims true *and* new to you",
        f"- **{WRONG_AT_VERIFIED_ALLOWED}** claims wrong at `verified`",
        f"- {len(sheet.claims)} claims, {len(sheet.verified_claims)} at `verified`",
        "",
    ]
    if sheet.caveats:
        lines.append("**What this run could not see:**")
        lines.extend(f"- {c}" for c in sheet.caveats)
        lines.append("")

    last = None
    for claim in sheet.claims:
        if claim.section_id != last:
            lines += ["", f"## {claim.section_title}", ""]
            last = claim.section_id
        mark = "!" if claim.at_verified else " "
        where = ", ".join(v.location for v in claim.anchors) or "no anchor"
        lines.append(f"- [{mark}] {claim.text}")
        lines.append(f"      `{claim.confidence}` · {where}")
    lines += ["", "---", "", "novel+true: ___    wrong at `verified`: ___    knew: ___"]
    return "\n".join(lines)
