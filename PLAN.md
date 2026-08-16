# kreb — build plan and status

Living document. Updated as work lands. The architecture it implements is
[`idea/docs/architecture.md`](idea/docs/architecture.md); this file tracks *where we are*
against it, not what it says.

**Last updated:** 2026-08-16 · 283 tests green · `provider/` + `budget/` landed

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
| 1 | `index/` symbols + hashes | **done** | 52 tests; mutation-verified |
| 2 | `store/` artifact DAG | **done** | two node classes + 4 detectors |
| 3 | `repo/` pinned-SHA access | **done** | secrets, shallow, dirty, vendored |
| 4 | `index/` map + import graph | **done** | model-free, guarded structurally |
| 5 | `archaeology/` evidence chains | **done** | 50 tests; REST, not `gh` |
| 6 | `doc/` schema + validators + Gate A | **done** | 49 tests; 3 audits stay manual |
| 7 | `provider/` + `budget/` | **done** | 44 tests; spend is measured, not guessed |
| 8 | `research/` outline + writers + stitch | **next** | |
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
| Staleness blind to semantic change | three hashes; `text_hash` = tokens + comment-free structure | 52-case corpus, PY + TS | ✅ green, mutation-verified |
| Every commit invalidates everything | two node classes; identity vs validity | idempotence / reproducibility / isolation / precision | ✅ green, mutation-verified |
| `map` costs more than the whole budget | map is model-free; summaries lazily memoized | transitive import-graph guard + exploding provider | ✅ green |

### Review round 1 (hermes, on `index/` + `store/`)

Three real defects, all reproduced independently before fixing:

1. **`text_hash` was blind to control-flow changes.** Indentation is not a leaf,
   so moving a statement in or out of a block left the token stream identical.
   A stale section was re-served as fresh after a behaviour change — the same
   class of silent miss as the original bug, one level down. Fixed by folding a
   *comment-free* structure representation into `text_hash`.
2. **An empty verifying trace was vacuously valid** (`all(())` is `True`), and
   that state was reachable because `put()` wrote the artifact before its
   provenance. Fixed both ends: empty traces are invalid, and provenance is now
   the first write so the artifact's existence implies it.
3. **Named constants were not indexed**, so a section citing `MAX_RETRIES` would
   fail as a *fabricated anchor* — a hard Gate A failure — purely from an
   incomplete index.

Known limitations accepted: `text_hash` over-fires on redundant parentheses,
kwarg reordering and trailing commas (cost, not correctness); TS overload
signatures are not indexed separately.

Two more, contained rather than proven: validation laundering (positive
requirements only, retry-attempt instrumentation) and video redundancy
(structural validator + lexical overlap measure).

### Round 2 (self + advisor, on `archaeology/`)

The module's whole purpose is to not confuse last-touch with introduction, so
every defect here was a variant of that same confusion sneaking back in:

1. **A saturated pickaxe could be reported as `verified`.** `--max-count`
   truncates from the *recent* end, so a search that hits its limit cannot see
   the introduction at all — its oldest result is merely the oldest one looked
   at. Worse, blame would often corroborate it, since that commit really did
   touch the lines. Fixed by asking for one extra record to detect saturation,
   and capping confidence at `derived` when it happens: blame agreeing about a
   commit is not independent evidence about *earlier* commits.
2. **Reverts were file-scoped.** `find_reverts` works on a path, so attaching
   its results to a symbol would claim a decision was reconsidered for a
   function it never touched. Fixed by intersecting with the pickaxe's commit
   set, and cached per file so a 40-symbol module does not re-walk 40 times.
3. **403 is overloaded by GitHub** — rate limit *and* permission denial. Telling
   someone to wait an hour for a wall that will still be there is a confidently
   wrong instruction, so the rate-limit headers now decide which it is.

Checked and *not* reproduced: `git log -S <needle>` with a leading-dash needle.
git's parse-options consumes the option's argument unconditionally, so the
separate form is safe. Kept, with a regression test.

All three fixes are mutation-verified: reintroducing each bug fails a test.

### Round 3 (hermes, on `archaeology/`)

The reviewer found one defect; chasing it surfaced a larger one it had not named.

1. **Pointer dereferences were read as comments.** The `\*` alternative in
   `_UNDISTINCTIVE` matched any line starting with `*`, which is a C block-comment
   continuation *and* a dereference. A deref-heavy body then had no needle and
   fell through to the blame-only branch. Operators now separate them.
2. **A later rename became the introduction.** The pickaxe finds when a line's
   *current* text first appeared, so any renamed line dates itself to the rename
   — and blame corroborates, so the wrong answer arrived at `verified`. Picking
   the single longest line made it a coin flip. Now up to three needles are
   searched and the oldest kept, ordered by `merge-base --is-ancestor` rather
   than by author date (which ties in the same second, is carried across by
   cherry-picks, and is forgeable).

Deliberately *not* done: downgrading confidence when needles disagree.
Disagreement fires equally on the correct case (symbol extended after
introduction) and the incorrect one (every line since rewritten), so it is not
evidence — and spending confidence on it would make `verified` unreachable for
any symbol that was ever edited.

**Operational lesson: `hermes --yolo` runs git in the repo it is pointed at.**
This reviewer built its test scenarios as commits on `main` rather than in
`/tmp` as instructed, and its `git add -A` swept in-progress work into them.
Nothing was lost or pushed, but future review passes get a scratch worktree, or
a committed tree and a check of `git log` afterwards. Flag order also matters:
`hermes --yolo -z "<prompt>"` — `-z` last, or argparse eats the next flag.

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
- **Sync I/O with threads, not asyncio** (revises architecture.md §7, amended
  there in the same commit). Everything else is synchronous subprocess work, the
  load is dozens of concurrent requests rather than thousands, and an async port
  would colour `research/`, `render/` and `cli/` for no gain at this scale.
- **`budget` layers *below* `provider`.** A ledger holding `Usage` objects
  inverts the dependency and `MeteredProvider` then closes a cycle — it failed
  as an `ImportError` the moment `provider/` was written. Charges carry
  primitives; the ledger never interprets a role.
- **Cost is measured, never guessed.** OpenRouter returns `usage.cost` per
  generation with no request flag needed (verified against their docs
  2026-08-16; the old `usage: {include: true}` is deprecated). A fallback
  estimate is allowed but is flagged `cost_is_estimated`, and the estimated
  share of a run is reportable.
- **No default spend cap.** Every ceiling is optional. The engine does not
  truncate research to hit a number nobody chose.
- **A ceiling cannot be exact.** A call's cost is unknown until it returns, so
  `max_per_run` means "start no new call once passed"; overshoot is bounded by
  one call, and that bound is what the test asserts.

## Environment notes

- Python 3.14.6; `tree-sitter` 0.26.0 + `tree-sitter-language-pack` 1.14.3 verified working.
- **`gh` is not installed.** Archaeology uses the REST API over stdlib `urllib`
  instead — no dependency, and `ForgeStatus` reports what it could not reach.
  Unauthenticated is 60 req/h, which is a hard wall for any real run; set
  `GITHUB_TOKEN` for 5000. Batching commit→PR lookups via GraphQL
  `associatedPullRequests` is the known next step if that becomes binding.
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
