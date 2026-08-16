# kreb — architecture v0.2

**Status:** proposal, revised after adversarial review
**Supersedes:** `architecture-draft.md` (v0.1)
**Given:** open source, built for the author's own use, no monetization. All three renderers in scope. Python engine, `uvx`, tree-sitter, OpenRouter, shelling out to `git`/`d2`/`ffmpeg`/`piper`.

v0.1 was reviewed adversarially and had three fatal flaws. This version fixes them. Findings verified by experiment are marked ✅.

---

## 0. Technology policy: Python, and no JavaScript runtime

**The engine is Python. Nothing in the toolchain may require Node.** Non-Python *binaries* invoked as subprocesses are fine — `git`, `d2` (Go), `ffmpeg` (C), `piper` (C++) — because they are single executables with a stable CLI, not language ecosystems that drag a runtime, a package manager and a lockfile into the install path. A Node dependency would break the one-line `uvx kreb` promise.

Three places JavaScript would otherwise have crept in, and what replaces it:

| Tempting | Why it's out | Instead |
|---|---|---|
| `ts-morph` / `typescript-language-server` for TS re-export resolution | needs a Node runtime alongside Python | tree-sitter in Python + honest ambiguity reporting (§5) |
| mermaid CLI for diagrams | needs puppeteer, i.e. a headless browser | `d2` — single Go binary, native SVG, real auto-layout |
| highlight.js inlined in the HTML output | ships ~200–300KB of JS into every artifact | **Pygments** — pure Python, server-side (see below) |

**The rendered HTML is therefore zero-JavaScript**, which turns out to be achievable with no loss:

- click-to-expand code → `<details>`/`<summary>`, native HTML
- diagrams → inline SVG from `d2`
- light/dark → CSS `prefers-color-scheme`
- syntax highlighting → Pygments spans, emitted at render time

The tooling survey preferred highlight.js on the grounds that raw `<code>` diffs more cleanly in git. **That argument was weaker than it looked:** PRD §6.5 tracks `.kreb/docs/` (the *markdown*) and ignores media — the markdown research doc is what gets committed and reviewed, not the rendered HTML. Diff-cleanliness of the HTML barely matters, so the pure-Python, zero-JS, no-CDN option wins outright. `tooling-survey.md` is corrected accordingly.

**Note on scope:** parsing *TypeScript and JavaScript source* remains a v1 target (PRD §6.1) — that is kreb reading JS, not kreb running on JS. Unaffected by this policy.

---

## 1. The organizing idea: an artifact DAG with **two node classes**

Everything kreb produces is a node in a DAG of derived artifacts. v0.1 said every edge was "a pure function of (input hash, params, model config)." That is true for half the graph and false — expensively false — for the other half.

**The research node's input set is not knowable before it runs.** The agent decides what to read via tool calls. A section's `depends_on` is an *output*, not an input. This is the classic dynamic-dependency case: it cannot use deep input hashing, and pretending otherwise is what made v0.1 invalidate the entire product on every commit.

So there are two node classes, and they are distinct types in the code:

| | `DeterministicNode` | `GeneratedNode` |
|---|---|---|
| Examples | `index`, `map`, `diagram`, `timing`, `mux`, `html` | `section`, `beats`, `narration`, `storyboard` |
| Key | `sha256(inputs ‖ params ‖ code_version)` | `sha256(node_id ‖ brief ‖ prompt_hash ‖ model_id ‖ params ‖ schema_version)` — **deliberately excludes repo state** |
| Freshness | recompute and compare | **verifying trace**: the set of `(symbol_ref, text_hash)` the writer actually read |
| Property | byte-reproducible | hit iff key matches **and** every trace entry still hashes equal |

The distinction is between **identity** (is this the same request?) and **validity** (is the answer still true?). v0.1 collapsed both into the word "hash." Splitting them is the highest-value change in this document: a README typo now invalidates nothing, and a change to `RetryPolicy.backoff` invalidates exactly the sections that read it.

```
repo@sha ─▶ index ─▶ map ─┐
                          ├─▶ outline ─▶ section×N ─▶ manifest ─┬─▶ html
   diagram_spec ─▶ diagram ┘                                     │
                                                                 ├─▶ beats ─┬─▶ narration_audio ─▶ audio ─▶ timing
                                                                 │          │                                  │
                                                                 │          └─▶ storyboard ─▶ narration_video ──┴─▶ video
```

### The section is the unit; the doc is a manifest

v0.1 made `doc` a single node. That breaks the thing §6.6 of the PRD explicitly promises — hitting the budget ceiling "stops cleanly and persists partial work… the run is resumable." One blob node means a run dying at 80% produced nothing, so resume is a full re-run.

Making `section@id` the unit collapses six separate problems into one mechanism:

- partial results become the normal case, not an error path
- `--depth 2` and `--focus retry-policy` (§6.4) are cache-free *selections over the manifest*
- staleness is per-section, which is what §6.3 wanted anyway
- parallel section writers become possible
- budget-stop resumption works
- re-rendering one paragraph re-renders one paragraph

It is also the only decomposition under which v0.1's headline claim — *resumption is the same code path as a cache hit* — is actually true.

### Keys must include the things that change most often

Not in v0.1's key, and both are silent-corruption bugs:

- **`sha256(prompt_template)`** — you ship via `uvx`, so users upgrade silently. Edit a prompt in v0.4 and every v0.3 artifact is served from cache as if nothing changed. The stale output is *plausible*, which is why this one is hard to notice.
- **`schema_version`**, in the store path, so a version bump cannot misread old artifacts.

Every artifact gets a `provenance.json` beside it: model id, **resolved OpenRouter provider slug**, params, temperature, template hash, usage/cost, response id, timestamp. The provider slug matters because OpenRouter serves one model id from several upstreams at different quantizations — two artifacts with the same key are otherwise not the same function. (The tooling survey already forbids the auto-router for cache-affinity; the same discipline buys reproducibility.)

### Consequences worth stating

- **Nondeterminism + caching = permanent fixation.** First result wins forever; a bad section is re-served identically on every rerun. Need `kreb regen <section-id>`, which bumps an `attempt` counter into the identity key and keeps both versions for diffing. Also cache **negative** results, or every resume re-pays for sections that were always going to fail.
- **Pin the commit.** A run takes 20 minutes; the user keeps coding. Resolve `repo@sha` once at start and read all content through `git cat-file` at that SHA — never `open()` on the working tree. Dirty tree → record `dirty: true, base: <sha>` and surface it.
- **Atomicity.** Write tmp+rename the moment a node validates. Never hold the doc in memory to the end, or SIGINT loses the run the budget design promised to preserve.
- **Determinism hygiene.** `temperature=0`, pass `seed` where supported.

### Two tests that keep this honest, written before anything else

1. Materialize the full DAG twice — the second run must make **zero** provider calls and **zero** `gh` calls.
2. Delete any single deterministic node, re-materialize, assert **byte-identical**.

If either fails the abstraction is leaking, and you find out in week two instead of month six.

---

## 2. Staleness — measured, and rebuilt ✅

The tooling survey recommended `sha256(node.sexp())`. **I tested it against tree-sitter 0.26; the reviewer independently tested it and extended the cases. It is broken.** The S-expression carries node types and field names only — no token text, no anonymous operators:

```
(assignment left: (identifier) right: (integer))
```

`(integer)` is identical for `3` and `5`. `(comparison_operator (identifier) (identifier))` is identical for `a == b` and `a != b`.

| Change | `sexp` | token-normalized |
|---|---|---|
| reformat / whitespace | unchanged ✓ | unchanged ✓ |
| comment added | **CHANGED ✗** false positive | unchanged ✓ |
| `retries = 3` → `5` | **unchanged ✗ silent miss** | CHANGED ✓ |
| `<` → `<=`, `==` → `!=` | **unchanged ✗ silent miss** | CHANGED ✓ |
| `retry_linear()` → `retry_exponential()` | **unchanged ✗** | CHANGED ✓ |
| `@cached` → `@retry` | **unchanged ✗** | CHANGED ✓ |
| TS `a: string` → `a: number` | **unchanged ✗** | CHANGED ✓ |
| statement added | changed ✓ | changed ✓ |

It is **simultaneously too insensitive and too sensitive**. And the trap is specific: Gate A's synthetic-refactor test uses `rope` to rename a symbol, which *does* perturb call-site structure enough to fire — **so Gate A passes while the mechanism is broken for every edit that matters.** You would ship believing it works.

**Three hashes per symbol, ~30 lines:**

```python
def token_hash(node, src, skip=("comment",)):
    """Ignores formatting and comments; catches every literal, operator, identifier."""
    toks = []
    def walk(n):
        if n.child_count == 0:
            if n.type not in skip:
                toks.append(src[n.start_byte:n.end_byte])
        else:
            for c in n.children:
                walk(c)
    walk(node)
    return hashlib.sha256(b"\x00".join(toks)).hexdigest()
```

- **`text_hash`** (above) — **drives staleness.**
- **`shape_hash`** (the sexp) — demoted to a *classifier*. text changed + shape unchanged → cosmetic-or-constant, render as "changed"; both changed → structural, flag hard.
- **`signature_hash`** — name, params, annotations, decorators, return type. Drives **caller** invalidation: a body change invalidates sections citing the symbol; a signature change additionally invalidates sections citing its callers. Without this tier you either miss caller staleness or flag the transitive closure and drown the signal.

**Four states, not a boolean:** `fresh` / `stale` / `moved` / `broken`. On a dangling anchor, search the index for an equal `text_hash` elsewhere — found means the function moved, so rewrite the anchor and don't flag. A repo-wide rename is classified via `shape_hash` as `renamed`, anchor rewritten, noted, not flagged. Otherwise every section fires at once and you've built the "your docs are out of date" banner nobody reads.

**Staleness is computed, never stored.** Compare recorded trace hashes against the current index at read time. Free, always correct, makes `kreb status` read-only.

**Two API corrections** ✅: `Node.sexp()` **does not exist in tree-sitter 0.26** (use `str(node)`), and the PyPI distribution is **`tree-sitter`**, not `py-tree-sitter`. Code written from the survey's line would not have run.

---

## 3. The three renderers: share the *plan*, not the prose

v0.1 shared a single `narration` artifact between audio and video. That is wrong, and it's wrong in a way that would have produced the exact failure §9.1 calls a hard constraint.

PRD §8.1 wants an audio script that is **self-contained**. §9.1 wants video narration that **complements** the slide and never restates it. These are not two selections from one text — they have *inverted* content requirements:

> Audio, correct: *"The retry policy lives in `src/http/retry.py` and does three things: caps at five attempts, backs off linearly, gives up on 4xx."*
>
> Video, with those three items as diagram labels on screen: narrating that sentence **is** the redundancy violation.

You cannot derive the video line from the audio line by deletion, because the correct deletion is exactly what the slide carries — and what the slide carries isn't known until the storyboard exists. **v0.1's edge direction was backwards:** `narration → storyboard` makes visuals a function of prose already written to be self-sufficient, so every slide duplicates its narration. The architecture would have guaranteed the failure.

**Fix — three artifacts:**

- **`beats`** — *shared.* Ordered `(section_id, key_point, confidence, hedge_required)`. The content plan. This is what's genuinely common, and beat selection and ordering is the hard part; sentence shaping is a prompt profile.
- **`narration_audio = f(beats, doc)`** — self-contained prose. §8.1 satisfied.
- **`storyboard = f(beats, doc, diagram_specs)`** → **`narration_video = f(storyboard)`** — written *against* known on-screen content. §9.1 becomes satisfiable.

Cost: one extra `narrate`-role pass for video. Cents.

### The honest claim about video

Naming `storyboard` does **not** remove the second authoring pass. Video still needs its own content model, prompts, and quality bar. What the naming buys is **containment**: typed, cached, diffable, staleness-tracked, confined to one module, and — the real prize — **validatable**.

So the defensible claim is *"video is a second authoring surface; here is where it lives and here is the validator that keeps it in bounds,"* not *"this makes video a dumb renderer."* The part of PRD §35 that survives is the part that matters: **no new facts enter downstream of the doc.**

### Timing: not a layering flaw, an artifact-decomposition error

The apparent cycle (storyboard needs durations, durations come from audio) exists only because v0.1 put `duration` *inside* the storyboard. Remove it:

```
beats ─┬─▶ narration_audio ─▶ audio ─▶ timing (ffprobe) ─┐
       │                                                  ├─▶ video
       └─▶ storyboard ─▶ narration_video ─────────────────┤
doc ─▶ diagram_spec ─▶ diagram ────────────────────────────┘
```

Storyboard scenes carry a `min_duration` **constraint**, never an actual duration. `timing` is a trivial deterministic `ffprobe` node. `video` is a **join**. Acyclic, independently cached.

Two rules fall out:

- **Duration is a computed field, structurally impossible to author.** If the model can emit a scene length it will, and it will be wrong, and you get drift eight minutes into an mp4.
- **Scene granularity must equal narration-segment granularity** (1–2 sentences), not section granularity — otherwise a 90-second section becomes one 90-second slide, which is the boring-deck failure mode.

Mux rule: `scene_len = max(audio_len, min_duration)`, pad with silence.

### The validator to keep

`visual == code && visual_mode == narrate` → **hard validation error.** Converting §9.1 from a prompt instruction into a type error is the single best idea carried over from v0.1, and it's the kind of rule that stays true at 2am.

**But be honest about what the narration validator covers.** "Introduces no new symbol references" is worth having and mechanically checkable — yet the real risk is *new claims about existing symbols*. *"…`RetryPolicy`, which is why the client is fast"* adds zero new symbols and one fabricated causal claim. Not mechanically detectable.

The general principle, which decides several designs below: **positive lexical requirements are enforceable; negative semantic ones are not.** So: every narration segment whose source section is `speculative` **must** contain a hedge from an allowlist (§8.2 requires this; nothing in v0.1 enforced it). Asserting presence is robust. Asserting absence is not.

---

## 4. Validation rules, rebuilt

**Rule 1 and Rule 3 are different rules** and should be reported separately, because they diagnose different bugs:

1. **Misplaced anchor** — symbol doesn't resolve *at the cited path*.
2. **Fabricated anchor** — symbol exists nowhere in the index.

Both are hard fails; a real symbol cited at the wrong path passes one and fails the other.

**Rule 1 is weaker than it reads.** It checks a symbol *exists*, not that it *supports the claim*. A model citing a real symbol and lying about its behaviour passes cleanly. Gate A's "`verified` claims that are actually inferred ≤5%" is untouched by any structural rule and remains a manual audit. Say so rather than implying coverage that isn't there.

### Rule 2 — the repo-scoped-verb linter cannot work, and is actively harmful

v0.1 proposed rejecting "repo-scoped verbs" in `background` sections. Three independent failures:

1. **The distinction is semantic, not lexical.** *"The client retries three times"* vs *"Clients typically retry three times"* share every verb.
2. **`background` sections are about behaviour by definition.** *"The library retries with exponential backoff by default"* is exactly the sentence §6.1 calls the highest-value output the tool can produce — as the left half of the library-default-vs-repo-override pairing. A verb linter fires on precisely the sentences you most want.
3. **The killer: you have a retry-and-validate loop, so the generator optimizes against the linter.** The model rewrites until it passes, laundering the claim into a blessed form and stamping it valid. **A cheap negative-semantic check under retry pressure is worse than no check**, because it manufactures confidence.

**Replace with structural rules:**

- Any section mentioning an identifier present in the symbol index **must carry ≥1 non-`external` Evidence item.** Identifier detection is mechanical (backticked tokens + CamelCase/snake_case, looked up in the index). Hard to evade, because evading means not naming the repo's symbols — which is the desired behaviour for a background section. This enforces §6.1's actual rule rather than a lexical proxy.
- A `background` section may cite **zero** repo symbols. Trivially checkable.
- **Containment at render time beats detection.** `background` renders in a visually distinct block labelled *"About the library, not this repo"*; in narration it gets a mandatory spoken prefix. Even if a claim slips through, the reader is told what kind of statement it is.

**Degraded languages have no symbol index, so Rules 1 and 3 cannot fire** — and those docs look identical to protected ones. That's what the capability manifest (§7) is for.

---

## 5. Scale and cost

### `map` must be model-free

PRD §6.1 specifies progressive leaf-to-root summarization of every module. Costed on a 50k-file repo that is 50k+ model calls before a single research question — hours of wall clock and several times the entire $2 Gate D budget *for the map alone*. It's the thing that dies first at scale.

**Make `map` deterministic:** directory tree + symbol index + top-N symbols by pagerank/churn + LOC and sizes. All already available from `index/`, at zero cost, incrementally updatable.

**Module summaries become a lazily-populated cache** — the agent requests a summary for a directory it is drilling into, memoized as its own node keyed on child symbol hashes. You pay for the ~20 directories actually visited, not 5,000. This fixes invalidation for free, since each summary depends only on its own subtree.

### Archaeology: the binding constraint is wall-clock, not rate limits

Forge limits are the easy half — GitHub GraphQL's `Commit.associatedPullRequests` batches ~100 commit→PR lookups per query, turning 300 REST calls into 3.

The real constraint is local git. `git log -S` walks the entire history diffing every commit; `git log -L` is O(history). On a 200k-commit repo that's minutes *per symbol*, and none of it appears in cost accounting because it costs $0 and hours.

1. `git commit-graph write --reachable --changed-paths` once at index time — Bloom filters make pathspec-limited walks ~an order of magnitude faster.
2. **Always pathspec-limit** (`-- path/to/file`), never bare. Avoid `--pickaxe-regex`, which defeats the bloom filter.
3. Bound it: `--max-count`, `--since`, per-section wall-clock budget degrading to "no archaeology found" rather than hanging.
4. Run archaeology **only for `kind == rationale` sections.** Structure sections don't need it.
5. Archaeology results are deterministic → cache as their own nodes keyed on `(symbol text_hash, head sha)`.

**Detect shallow clones.** On a `--depth 1` checkout there is no history, archaeology silently returns nothing, and the doc degrades into paraphrasing the code — the exact Gate B failure this product exists to avoid, arriving silently. Check `.git/shallow` and hard-warn.

### The cross-file resolver is *not* on the critical path

v0.1 put it first because "everything depends on it." Examining what actually consumes resolution:

- validating an anchor → **definition index** (tree-sitter tags), no resolution
- staleness → subtree hashes, no resolution
- the map → module-granularity import graph, re-exports degrade to file-level edges
- "find callers of X" during archaeology → `git grep`/ast-grep on the name, high recall, false positives filtered by the model at zero marginal cost

Only extracted call-path diagrams genuinely need precision, and they're late.

Worse, a hand-rolled resolver's failure mode is **strictly worse than not having one**: Python `__getattr__` hooks, conditional imports, `importlib`, TS `export * from` chains are undecidable in general, so it will be wrong in a long tail — and its wrongness is silent and plausible, emitting a confident anchor at the wrong definition. That's a fabricated anchor Gate A cannot catch, because the symbol resolves.

**v1: definition index + name-based resolution that reports ambiguity honestly** (two matches → carry both, or drop to `derived`). Build the resolver later, driven by observed anchor failures on the corpus.

For TypeScript specifically, resolution stays in Python on tree-sitter — `export * from` chains get resolved as far as the import graph allows and report ambiguity beyond that. (`ts-morph` would resolve barrel files correctly but drags in a Node runtime; see §0.)

---

## 6. Module layout

```
kreb/
  store/        content-addressed artifact store: keys, atomic writes, provenance, GC
  repo/         pinned-SHA access (git cat-file), shallow detection, ls-files enumeration
  index/        tree-sitter symbol index, import graph, the three hashes
  archaeology/  blame-through → pickaxe → forge evidence chains
  research/     outline planner, section writers, the loop
  doc/          typed schema, validators, manifest
  viz/          diagram_spec → SVG via d2 subprocess
  provider/     OpenRouter transport + usage reporting
  budget/       ceilings, per-phase accounting, stop decisions
  external/     deps/domain fetch + TTL cache
  jobs/         SQLite registry, resumption
  config/       kreb.toml + secrets resolution
  tts/          TTS port (piper subprocess / hosted)
  render/
    html/       projection
    beats/      doc → shared content plan
    narration/  beats → narration_audio | storyboard → narration_video
    audio/      narration_audio → wav + timing
    storyboard/ beats + doc + diagram_specs → scenes
    video/      join(storyboard, narration_video, audio, timing, diagrams) → mp4
  cli/
  mcp/          deferred
```

**Changes from v0.1 and why:**

- **`store/` added.** v0.1's central idea — the artifact DAG — had no module.
- **`repo/` added.** PRD §3.2 names it as an injected port; v0.1 lost it, which is how the dirty-tree bug got in.
- **`viz/` extracted, and this fixes a real violation.** Extracted diagrams come from AST traversal, but *both* html and video need them — so either `render/html/` imports `render/video/`, or something under `render/` imports `index/`, violating v0.1's own rule. Fix: **`diagram_spec` (d2 source + `extracted|asserted` provenance) is produced during doc construction, where `index/` access is legal, and stored in the doc.** Renderers consume it like any other section content. This preserves the rule *and* makes provenance travel into every renderer, which §9.3 requires anyway.
- **`budget/` split from `provider/`.** v0.1 conflated transport, policy and accounting. Budget enforcement is a property of the run (§6.6 requires clean stop + persisted partials, and renderer spend accounted separately) — so either renderers can't enforce the ceiling or `provider/` needs job state. Provider reports usage; budget owns ceilings and the stop decision.
- **`tts/` lifted out of `render/audio/`** — §3.2 lists it as a port; swapping piper for hosted shouldn't drag the renderer.
- **`external/`, `config/` added** — both required by the PRD, both absent from v0.1.

**The dependency rule is layered, not a whitelist.** v0.1's "render/* may import doc/ and provider/" said nothing about ordering *within* render, where real edges exist. Declare:

```
store < repo < index < archaeology < research < doc < viz
      < render/beats < {render/narration, render/storyboard}
      < {render/audio, render/html} < render/video < cli
```

Enforce with `import-linter` layered contracts in CI — a config file that catches ordering violations a cycle check passes.

**The claim that survives:** deleting `render/video/` leaves a working product. True once diagrams move to `viz/`. This is the answer to "will the renderers destabilize the core," and it holds.

---

## 7. Safety and honesty

### Secrets — one v0.1 decision was backwards

PRD §6.5 puts `kreb.toml` at the repo root so `.kreb/`'s ignore rule doesn't swallow it — which makes it **the file most likely to be committed.** It must be structurally incapable of holding `OPENROUTER_API_KEY`: env var or keyring only, and reject the key with a loud error if it appears in TOML.

Worse: **the research agent reads the repo and quotes it into an artifact you publish or commit.** `.env` files, `config/secrets.yaml`, fixtures with live tokens, a hardcoded key in a 2019 commit that archaeology surfaces.

- Enumerate files via `git ls-files` so `.gitignore` is respected by construction — this also solves vendored/generated exclusion for free.
- Hard denylist: `.env*`, `*.pem`, `*.key`, `id_*`.
- Entropy/regex scrub on any code excerpt before it enters a section body.

For an open-source tool whose output people commit, this is not optional.

### Capability manifest

When `gh` isn't authenticated, archaeology silently produces no PR evidence and **the doc looks exactly as authoritative with less behind it.** Same for shallow clones, `research.web = off`, and degraded-language files.

Every doc carries, and every renderer surfaces:

```json
{"git": "ok", "forge": "none", "web": "deps", "languages": ["py"],
 "degraded_files": 412, "dirty": false, "base_sha": "8d9e1a…"}
```

~50 lines, and it directly serves the one thing that differentiates kreb from DeepWiki.

### Concurrency, decided once

`asyncio` for network (provider, `gh`); `ProcessPoolExecutor` for tree-sitter parsing (CPU- and GIL-bound); SQLite in WAL mode, single writer.

Hazards: parallel `git` subprocesses contend on `.git/index.lock` — use `--no-optional-locks` and read-only plumbing only; parallel section writers must share one `gh` rate-limit budget or they burst through 5,000/h; two `kreb` runs in one repo need a lockfile in `.kreb/`.

### Smaller holes worth closing

- Monorepo: whole-repo default scope is wrong above a size threshold — default to the nearest package and say so.
- Vendored/generated code (`vendor/`, `*_pb2.py`, minified bundles) will dominate pagerank and produce `verified` anchors into generated files. Honour `.gitattributes` `linguist-generated`/`linguist-vendored` — free signal, already present in many repos.
- Submodules off by default; blame across a submodule boundary is meaningless.
- `.kreb/` grows unboundedly with media — needs `kreb gc` and a size cap.
- **Budget accounting must count failed retry attempts**, or the ceiling under-counts by up to 3×.
- Windows: no easy piper binary, ffmpeg path assumptions. Declare unsupported rather than half-supporting.
- **Specify `SymbolRef` before writing the index.** `path#SymbolName` is ambiguous for methods, overloads, nested functions, and every `__init__`. Use `path#Class.method` with a documented disambiguation rule. It appears in every artifact and is painful to migrate.
- TTS cache key must include the **piper binary version and voice model hash**, or upgrading piper plus editing one paragraph yields an audible timbre change at one segment boundary.

---

## 8. Build order

1. `store/` + `repo/` + `index/` + the three hashes. **No model calls.** Property tests with `hypothesis`; the two DAG tests from §1.
2. `archaeology/` + evidence chains + commit-graph + bounds. Still deterministic.
3. `doc/` schema + validators + Gate A harness.
4. `research/` — outline → sections → manifest. First model calls.
5. `render/html/` + `viz/`.
6. **Gate B on a repo you know cold. Stop here if it fails.**
7. `render/beats/` + `narration_audio` + `audio/`.
8. `storyboard/` + `narration_video` + `video/`.
9. `mcp/`.

Steps 1–3 carry roughly half the risk with zero LLM nondeterminism.

---

## 9. Verification plan for the three fatal bugs

All three fatal findings share one property: **they fail silently.** Nothing crashes, the output looks plausible, and the guarantee you were relying on simply isn't there. One of them would have passed its own acceptance test. So the mitigation is not the fix — it is a **detector per bug, written before the implementation.** A test written after the code tends to encode what the code does rather than what it should do.

Current status is honest: **one of five is empirically verified. The rest are designs that could themselves be wrong.**

| # | Bug | Fix status | Detector status |
|---|---|---|---|
| 1 | staleness blind to semantic change | ✅ verified by experiment | not yet written |
| 2 | every commit invalidates everything | design only | not yet written |
| 3 | `map` costs more than the budget | design only | not yet written |
| 4 | validation rule laundering | design only | not yet written |
| 5 | video narration restates slides | design only | deferred to step 8 |

### Bug 1 — staleness. Detector: a semantic-change corpus

A golden table of `(before, after, must_fire)` triples, asserted exactly. Deterministic, no model calls, milliseconds to run.

Minimum cases, in **both Python and TypeScript**, since the reviewer found TS-specific misses (`a: string` → `a: number`, `&&` → `||`):

- must fire: integer/string literal change, comparison operator, arithmetic operator, boolean operator, callee name, decorator, keyword-argument name, attribute access, type annotation, added/removed statement, added/removed parameter
- must **not** fire: reformatting, whitespace, comment added/edited/removed, line wrapping

**The rule that matters: the `rope` rename case is one row in this table, never the whole test.** Relying on it alone is precisely what produced the false pass. If the corpus has fewer than ~20 rows, it is not yet a test.

Second detector, for the classifier: assert `shape_hash` is unchanged for the cosmetic-or-constant rows and changed for the structural ones. That's what licenses rendering "changed" rather than "may be wrong."

### Bug 2 — cache invalidation. Detector: four DAG properties

The first two prove the abstraction holds at all; the second two are the direct regression tests for this bug.

1. **Idempotence.** Materialize the full DAG twice — the second run makes **zero** provider calls and **zero** `gh` calls.
2. **Reproducibility.** Delete any single deterministic node, re-materialize, assert **byte-identical**.
3. **Isolation.** Touch a file no section depends on (a README typo). Assert **zero** sections invalidate. *This is the bug, stated as a test.*
4. **Precision.** Change one symbol's body. Assert exactly the sections whose trace contains it invalidate, and **no others**.

Property 4 is the one to write first, because it fails in both directions — under-invalidation is a stale doc, over-invalidation is the cost explosion — and a single assertion catches both.

Add a fifth once prompts exist: **edit a prompt template, assert affected generated nodes miss cache.** That's the silent-upgrade bug, and it is invisible without an explicit test because the stale output is plausible.

### Bug 3 — map cost. Detector: a call-count assertion, not a scale test

You do not need a 50k-file repo. The property is about *call counts*, testable at unit level with a fake provider that counts invocations:

- **Assert model calls during `map` construction == 0.** The map is deterministic; any nonzero count is the bug returning.
- **Assert summary calls ≤ number of directories the agent actually visited.** Catches an accidental eager walk.
- Add a synthetic wide tree (a few thousand generated files, cheap to create) and assert map construction stays under a wall-clock bound.

Also instrument what the budget cannot see: **wall-clock per archaeology call**, with the per-section budget degrading to "no archaeology found" rather than hanging. Git time costs $0 and hours, so it is invisible to cost accounting by construction.

### Bug 4 — validation laundering. Mitigation is a design rule plus instrumentation

The structural replacement (identifier in index ⟹ non-`external` evidence; `background` cites zero repo symbols) is enforceable. But the general lesson needs writing down as a standing rule:

> **Never put a negative-semantic check behind a retry loop.** Under retry pressure the generator optimizes against the checker and launders the claim into an approved form. Prefer positive requirements (presence of a hedge, presence of evidence) which cannot be satisfied by rephrasing.

Instrumentation, cheap and useful: **record the attempt number on every section.** A section that passed only on attempt 2 or 3 is a quality signal — laundering leaves a trace in the retry count. Surface it in `kreb status` and sample those sections first in the Gate A manual audit.

### Bug 5 — video redundancy. Detector: lexical overlap

The hard validator (`visual == code && visual_mode == narrate` → error) is structural and holds. Add one measurable check: **compute token overlap between a scene's on-screen text and its narration.** High overlap means the narration is reading the slide, which is the §9.1 violation in its quantitative form. Threshold tuned on real output, reported rather than enforced at first.

### Why the build order is itself the mitigation

Bugs 1, 2 and 3 all live in `index/`, `store/` and `map` — **steps 1–3, which contain no model calls.** Every detector above is deterministic, fast, and free to run on every commit. That is the strongest structural mitigation available: the three fatal bugs are all falsifiable before a single nondeterministic component exists.

Bugs 4 and 5 live downstream in generated content, where certainty is not available — so they get containment (structural rules, positive requirements, instrumentation) rather than proof.

---

## 10. Open questions

- **Coherence across independently-written sections.** Planner + writers is forced by resumption, cache-prefix stability and depth-selection — but sections can't see each other and the doc may read like eight disconnected essays. Proposed fix: an explicit `outline` artifact where each section declares `covers` / `assumes` / `defers_to`, writers receive their assumptions as context, and a final **stitch** node at the `mechanical` role may edit *only* transitions and headings — validated to introduce zero new `Evidence` and zero new identifiers. Untested.
- **Does `beats` actually carry enough** for two renderers to diverge correctly, or does it collapse into a de-facto script? Test on one real section before building both renderers.
- **Is `text_hash` the right staleness granularity in practice**, or does normal refactoring churn still produce banner-blindness? Only measurable on a real repo over weeks.
- **Whether `verified` can be made to mean anything** without a human in the loop. Rule 1 proves existence, not support. This is the gap between Gate A and Gate B, and no structural rule closes it.
