# kreb — build plan and status

Living document. Updated as work lands. The architecture it implements is
[`idea/docs/architecture.md`](idea/docs/architecture.md); this file tracks *where we are*
against it, not what it says.

**Last updated:** 2026-08-16

---

## The goal

A working `kreb doc md` and `kreb doc pretty` on a real repository, producing a
symbol-anchored, confidence-tagged research document that passes Gate A
mechanically and can be judged against Gate B by hand. Then the renderers.

The measure of done is not "the code runs" — it is **Gate B**: on a repository
you know cold, does the document contain ≥3 true statements you did not already
know, and 0 confidently-wrong statements at `verified` confidence.

## Sequencing principle

Steps 1–3 contain **no model calls**. All three fatal bugs found in review live
there, so they are falsifiable deterministically, on every commit, for free,
before any nondeterminism enters the system. Nothing downstream is trustworthy
until they are green.

---

## Status

| # | Module | State | Notes |
|---|---|---|---|
| 1 | `index/` symbols + hashes | **done** | 46 tests; mutation-verified |
| 2 | `store/` artifact DAG | in progress | two node classes + 4 detectors |
| 3 | `repo/` pinned-SHA access | pending | |
| 4 | `index/` map + import graph | pending | must stay model-free |
| 5 | `archaeology/` evidence chains | pending | ⚠ `gh` not installed here |
| 6 | `doc/` schema + validators + Gate A | pending | |
| 7 | `provider/` + `budget/` | pending | first model calls |
| 8 | `research/` outline + writers + stitch | pending | |
| 9 | `viz/` + `render/html` | pending | |
| 10 | `cli/` | pending | |
| — | **Gate B** | pending | **stop-or-continue point** |
| 11 | `render/beats` + audio | pending | |
| 12 | `render/storyboard` + video | pending | |
| 13 | `mcp/` | pending | deferred deliberately |

## The three fatal bugs and their detectors

Each was found in adversarial review; each fails *silently*, so the fix is not
enough on its own — the detector is the deliverable.

| Bug | Fix | Detector | State |
|---|---|---|---|
| Staleness blind to semantic change | three hashes; `text_hash` drives staleness | 46-case corpus, PY + TS | ✅ green, mutation-verified |
| Every commit invalidates everything | two node classes; identity vs validity | idempotence / reproducibility / isolation / precision | in progress |
| `map` costs more than the whole budget | map is model-free; summaries lazily memoized | assert 0 model calls during map | pending |

Two more, contained rather than proven: validation laundering (positive
requirements only, retry-attempt instrumentation) and video redundancy
(structural validator + lexical overlap measure).

---

## Decisions already settled

Recorded so they are not relitigated:

- **Python, no Node runtime.** Non-Python single binaries as subprocesses are
  fine (`git`, `d2`, `ffmpeg`, `piper`); a JS ecosystem is not. Rendered HTML is
  zero-JavaScript.
- **The CLI is the integration boundary**, not MCP. No client implements the
  tasks extension yet.
- **All three renderers stay.** Video is a second authoring surface, contained
  in `render/storyboard/` and validated, not pretended away.
- **No monetization.** Built for the author's use, then open-sourced. Gate B is
  the only gate that matters.
- **Definition index, not a resolver, for v1.** A wrong resolver is worse than
  none: it emits confident anchors at wrong definitions, which Gate A cannot
  catch because the symbol resolves.
- **`research` role must be a cheap frontier model.** Sonnet-class pricing puts
  a depth-2 doc at ~$5, against a $2 target.

## Environment notes

- Python 3.14.6; `tree-sitter` 0.26.0 + `tree-sitter-language-pack` 1.14.3 verified working.
- **`gh` is not installed.** Archaeology must use REST/GraphQL directly or degrade.
- Push is over SSH as `Udreka13`.

## Open questions

Carried from research; none blocks the build, all bear on whether it is worth it.

1. **Does a coding agent with terminal access already answer "why is this code
   like this" well enough?** Untested. One afternoon settles it. The highest-value
   experiment available, because a negative result changes the wedge.
2. **Do independently written sections read as one document?** The stitch node
   is a guess.
3. **Can `verified` mean anything without a human?** Anchor rules prove a symbol
   exists, not that it supports the claim. No structural rule closes this.
