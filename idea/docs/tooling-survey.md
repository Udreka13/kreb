# kreb — tooling survey (build vs reuse)

**Date:** 2026-08-14
**Method:** 7 parallel `hermes` agents, each required to report license, stars, **last push date** and latest release for every library, with an explicit ADOPT / EVALUATE / AVOID verdict. Six returned 15–20KB reports (`w2-q1` … `w2-q6` in scratchpad); the seventh (audio/video) stalled, so §7 below is my own research instead. Load-bearing package claims were re-verified by me against PyPI.
**Companion doc:** [`pmf-research.md`](./pmf-research.md) — the market findings that set this scope.

> **Caveat carried from the agents:** GitHub's API rate-limited this host partway through several runs; some star counts came from rendered pages or shields.io instead. Version numbers and last-push dates came from PyPI/npm JSON and are reliable. Verify pins before committing them to `pyproject.toml`.

---

## The recommended stack, in one place

| Layer | Pick | Note |
|---|---|---|
| Parsing | `tree-sitter` 0.26.0 + `tree-sitter-language-pack` 1.14.3 | one abi3 wheel, 371 grammars |
| Symbol queries | vendored `tags.scm` + aider's `repomap.py` shape | Apache-2.0, attribute it |
| Git | shell out to `git` ≥2.39, pinned env | not a library |
| Archaeology | vendor `gitwhy`'s ~300-line algorithm | optionally shell to the binary |
| Forge | `gh` CLI (+`glab`) | **authenticated — 60/h unauth is a blocker** |
| Agent loop | hand-written, few hundred lines | no framework |
| Provider | `openrouter` 1.1.54 (typed `usage.cost`) | fallback: `openai` SDK at OR base URL |
| Schema | pydantic v2 + `json_schema` strict + retry-validate | no instructor, no outlines |
| Jobs | stdlib SQLite + atomic tmp+replace | no APScheduler, no Redis |
| CLI | `typer` 0.27.1 + `rich` 15.0.0 | strict exit-code contract |
| Config | `pydantic-settings` 2.15.0, TOML at repo root | pydantic is already a dep |
| Docs → HTML | `markdown-it-py` 4.2.0 + `jinja2` 3.1.6 | + `mdit_py_plugins` |
| Highlighting | ~~inlined highlight.js~~ → **Pygments** | corrected — see note below |
| Diagrams | **d2** v0.7.1 subprocess | fallback graphviz `dot` |
| Tests | pytest + `syrupy` + `hypothesis` | + `vcrpy` 8.3.0 / `pytest-recording` |
| Refactor fixtures | `rope` 1.14.0 + ast-grep | **AVOID bowler (dead)** |
| MCP | **defer**; `mcp` 2.0.0 when triggered | not FastMCP v3/v4 |
| TTS | `piper-tts` 1.6.1 local (⚠ GPL-3.0) | hosted fallback: OpenAI `tts-1` |
| A/V assembly | raw `ffmpeg` subprocess | **AVOID pydub + ffmpeg-python (both dead)** |

**Independently verified by me on PyPI (2026-08-14), not taken from agent output:** `mcp` 2.0.0 (official, LF Projects, uploaded 2026-07-28 — the spec date itself), `openrouter` 1.1.54 ("Official Python Client SDK for OpenRouter", uploaded 2026-08-13), `tree-sitter-language-pack` 1.14.3 (cp310-abi3 wheels for macOS x86/arm + manylinux aarch64), `tree-sitter` 0.26.0.

> ⚠️ **Package-name correction.** The agents called the binding **`py-tree-sitter`** — that is the *GitHub repo* name. The PyPI distribution is **`tree-sitter`**. `uv add py-tree-sitter` fails.

---

## 1. Symbol index — the gap *is* the product

The sharpest sentence from the whole survey:

> *"Nothing in the ecosystem gives you tree-sitter symbol index + cross-file re-export resolution + subtree staleness hashing end-to-end — that gap is essentially your product surface."*

So the split is clean: **adopt the parse engine, hand-write the resolver.**

**Adopt:** `py-tree-sitter` 0.26.0 (MIT) and `tree-sitter-language-pack` 1.14.3 — one abi3 wheel covering every grammar, which also keeps `uvx` install compile-free. Vendor `tags.scm` from tree-sitter-python 0.25.x / tree-sitter-typescript 0.23.x, and port the capture shape from aider's `repomap.py` + `grep_ast/tsl.py` (Apache-2.0 — attribute it).

**Avoid, with reasons:** `tree-sitter-languages` (frozen — this is the one most tutorials still recommend), `stack-graphs` (archived, confirmed), ctags (GPL, and no subtree access), Graft (too young, Node sidecar, and not a staleness engine), semgrep/ast-grep (wrong problem for this layer), Glean (ops burden).

**Hand-write (~1–2k LOC):** per-language symbol policy (~200 LOC), the cross-file import/re-export resolver, the index schema, and the canonical `path#SymbolName` layer.

**Staleness is nearly free:** `sha256(node.sexp())` — the s-expression of the AST subtree — is about ten lines. That's §6.3's whole mechanism, and it's the cheapest load-bearing thing in the design.

Optional: `networkx` pagerank if you want a "central symbols" section. Keep LSP (pyright/multilspy) as a *later verifier*, never the foundation.

## 2. Git archaeology — shell out, and never take the shortcut

**Use the `git` binary, not a Python library.** Pin the environment exactly as `gitwhy` does: `LC_ALL=C`, `TZ=UTC`, `GIT_CONFIG_GLOBAL=/dev/null`, `--no-pager`, `-c color.ui=false`, `--no-abbrev`, `--line-porcelain`. Requires git ≥2.39 for `--diff-merges=remerge` and Bloom commit-graph.

**The one rule that matters:**

> *"Never ship a 'last-touch = introduction' shortcut — that's the hallucination risk you're paying to avoid."*

`git blame` gives you who touched a line last, which is usually *not* who introduced the behaviour. The correct chain is **blame-through (`-w -M -C`) → pickaxe gate (`git log -S`, `-L`) → forge lookup → confidence decision-table.** Vendor gitwhy's ~300-line version of this as the Python fallback; optionally shell to the binary when present.

**Forge access — this is a real constraint.** `gh api repos/{o}/{r}/commits/{sha}/pulls --jq '.[0].number'` maps a commit to its PR. But **unauthenticated GitHub REST is 60 requests/hour, which the agent flags as an absolute blocker** for a multi-symbol doc; authenticated is 5,000/h. Require `gh auth login` and fail loudly without it. Tag every forge-derived claim with its retrieval method for provenance. Offline/no-forge mode degrades to regex over commit messages (`closes #N`).

Skip the churn/metrics tools — Hercules is stale, CodeScene is closed, PyDriller solves a different problem. Ranking by pickaxe-verified introduction is ~50 bespoke lines and is *better* than hotspot models here, which get introduction wrong.

## 3. Agent loop — write it yourself

Unambiguous verdict: **no LangChain, no LangGraph, no deepagents, no ADK, no LlamaIndex.** The loop is a few hundred lines; the frameworks are lock-in and dependency weight. (If you later want scaffolding, `pydantic-ai` slim — never as lock-in.)

**Cost accounting is solved cleanly:** the official `openrouter` PyPI package (v1.1.54) exposes a typed `usage.cost` — *actual charged USD*, not an estimate — which is exactly what §6.6 needs to be honest rather than guessed. Also track `cost_details.upstream_inference_cost` and `cached_tokens`.

**Caching, concretely:** stable first-message system head (repo map + conventions), one `session_id` per run, `cache_control` breakpoint if routed to Anthropic/Alibaba, **pin a concrete model** — no `auto`/pareto router, and no manual `provider.order`, both of which break cache affinity. Verify it worked by reading `cached_tokens` back.

**Structured output:** pydantic v2 → `response_format: json_schema` strict *when the endpoint advertises `structured_outputs`* — it isn't universal across OpenRouter models, so always parse with pydantic and retry-and-validate (≤2 re-prompts) regardless.

**Jobs:** stdlib SQLite registry + per-run workspace with atomic tmp+replace writes. Resume by replaying the same `session_id` and system head. Nothing heavier.

## 4. CLI, packaging, MCP

`typer` 0.27.1 (the MCP SDK itself uses it), `rich` 15.0.0 for progress, `pydantic-settings` 2.15.0 for `kreb.toml`. For `uvx`: `[project.scripts] kreb = "kreb.cli:main"`, hatchling backend, `requires-python >=3.10`; tree-sitter's cp310-abi3 wheels keep it compile-free on mac and linux.

**On MCP — defer, and the reasoning is sound.** The official `mcp` **2.0.0** SDK (released 2026-07-28) *does* speak the stateless revision — FastMCP v3 is legacy-era and v4 is beta, so the official SDK is the answer when the time comes. But:

- no mainstream client implements the tasks extension (searched; absence, not proof)
- no server-side tasks implementation ships in the official SDK
- **`mcp` 2.x serves every earlier revision from the same server**, so waiting costs nothing and loses no backward compatibility

> *"There is no compounding interest on building MCP early."*

Concrete trigger to revisit: a client you target ships tasks support, **or** a user asks "can my agent run kreb". Then it's a day of work wrapping the existing library — not a project.

## 5. HTML and diagrams

**Parse:** `markdown-it-py` 4.2.0 with `mdit_py_plugins` (front_matter, container, admonition, attrs). Typed per-section front-matter is ~40 lines of your own schema — don't import a library for it. `jinja2` 3.1.6 for the single-file shell, assets inlined as base64 `data:` URIs.

**Highlighting — ⚠️ this recommendation was wrong; use Pygments.**

The original reasoning: inlined **highlight.js** keeps raw `<code>` in the markup so the file diffs cleanly in git, whereas Pygments emits span-noise. **The premise is faulty.** PRD §6.5 tracks `.kreb/docs/` — the *markdown* — and ignores media; the markdown research doc is the artifact that gets committed and reviewed, not the rendered HTML. Diff-cleanliness of the HTML was never a real requirement, and trading it for ~200–300KB of inlined JavaScript in every artifact is a bad deal.

**Use Pygments** (pure Python, server-side, no CDN, no runtime). Combined with `<details>`/`<summary>` for collapse, inline SVG diagrams, and CSS `prefers-color-scheme` for theming, **the rendered page needs zero JavaScript.** See `architecture.md` §0. Model the single-file assembler on pytest-html.

**Diagrams — skip mermaid.** Rendering mermaid to SVG needs puppeteer/a headless browser, and there is no clean pure-Python path. Use **d2** (v0.7.1, MPL-2.0) as a subprocess binary — native SVG, real auto-layout, dark-theme support; graphviz `dot` as the fallback. The provenance distinction from §9.3 renders naturally: same renderer, **dashed/dimmed edges plus a "✱" chip and legend** for asserted diagrams versus extracted ones.

**Don't import the graph extractors.** pydeps, pyan3, madge, dependency-cruiser all duplicate what your tree-sitter pass already produces — and code2flow is unmaintained since 2023. Imitate their output conventions (dashed cycles, grouped clusters); don't take the dependency. The clean division: **you build the extractor, you reuse the renderer.**

## 6. Gate A harness — mechanical first, judge last

The key insight: **kreb's own symbol indexer is the factuality harness.** Anchor-resolution and fabricated-anchor checks are pure AST lookups — fully mechanical, no LLM, no judge. Build the indexer as a library rather than a script so the tests can call it directly.

**Be skeptical of the eval frameworks.** The agent's verdict on RAGAS / DeepEval / TruLens / Giskard / promptfoo / LangSmith: **do not wire any of them into the per-commit gate.** Most measure fluency and vibes, not grounding. Borrow the *idea* (claim→citation verification) for an optional, budgeted, human-audited semantic sample — never as a hard gate.

**Determinism and cost:** `vcrpy` 8.3.0 + `pytest-recording` 0.13.4 to record-once/replay-always the OpenRouter calls. That's what makes a suite over nondeterministic LLM output runnable on every commit at near-zero cost — the thing §14 step 2 needs to be true.

**Synthetic refactors** (to prove staleness fires): `rope` 1.14.0 for Python, ast-grep for TS, optional LibCST. **Avoid bowler — dead.** `hypothesis` for property-fuzzing the resolver and the staleness invariant; `syrupy` snapshots for the deterministic claim/HTML layer.

Lockfile truth source for the pinned-version rule: `uv.lock`.

## 7. Audio and video renderers

> The q7 agent stalled after writing its header, so **this section is my own research**, verified directly against PyPI and vendor pricing on 2026-08-14. It is thinner than the sections above — treat it as a starting point, not a completed survey.

### TTS — local is free and that decides it

Verified PyPI state:

| Package | Version | Last release | Verdict |
|---|---|---|---|
| `piper-tts` | 1.6.1 | **2026-08-13** | **ADOPT** — actively developed, fast, local. ⚠️ **GPL-3.0-or-later** |
| `edge-tts` | 7.2.8 | 2026-03-22 | EVALUATE — free and good, but it drives Microsoft's endpoint; ToS-grey for a shipped product |
| `kokoro` | 0.9.4 | 2025-04-05 | EVALUATE — ~16 months stale |
| `TTS` (Coqui) | 0.22.0 | 2023-12-12 | **AVOID** — company shut down; dead since 2023 |

**The GPL-3.0 on `piper-tts` is a genuine constraint** the PRD doesn't account for. Shelling out to the Piper *binary* as a subprocess keeps kreb's own license free; importing it as a library does not. Since kreb is a CLI that already shells out to `git`, `d2` and `ffmpeg`, subprocess is both the natural design and the licensing-safe one.

**Hosted cost for a 15-minute overview** (~2,250 words ≈ 13k characters):

| Provider | Rate /1M chars | Per 15-min doc |
|---|---|---|
| Local (Piper/Kokoro) | — | **$0.00** |
| OpenAI `tts-1` | $15 | ~$0.20 |
| OpenAI `tts-1-hd` / Deepgram Aura-2 | $30 | ~$0.39 |
| ElevenLabs Flash v2.5 | $50 | ~$0.65 |
| ElevenLabs Multilingual v3 | $100 | **~$1.30** |

That last row is the point: **premium hosted TTS would eat 65% of the entire $2/doc budget on narration alone**, for a renderer wave-1 research found little demand for. This vindicates two PRD decisions — §3.2's "local matters for cost" and §6.6's insistence that renderers be accounted separately from research. Default to local; make hosted an opt-in flag.

§8.2's per-section TTS cache keyed on script hash is what makes iteration survivable either way: a one-paragraph edit re-renders seconds, not the whole run.

**Unresolved and worth testing early:** pronunciation control for identifiers. "RetryPolicy", "IBNR" and "OAuth" mangled by a TTS engine will wreck the format faster than bad prose. Check what lexicon/SSML/phoneme control Piper actually offers before committing.

### Assembly — both Python wrappers are dead

| Package | Version | Last release | Verdict |
|---|---|---|---|
| `pydub` | 0.25.1 | 2021-03-10 | **AVOID** — 5 years stale |
| `ffmpeg-python` | 0.2.0 | 2019-07-06 | **AVOID** — 7 years stale |

Call `ffmpeg` directly via `subprocess`. Both wrappers are abandoned, and the raw command line is more legible for the concat-with-timing job anyway. Get exact per-segment durations from `ffprobe` and use them to drive both slide timing and WebVTT cue boundaries — **you already have the script, so align rather than transcribe.** No Whisper pass, no forced aligner.

### Video — the estimate holds, so scope it deliberately

I didn't get an independent survey here, and wave-1's structural argument stands: the doc contains no slide breaks, diagram placement or pacing, so video is a second authoring surface rather than a renderer. The cheapest credible version is **HTML slides generated from the section tree → screenshot per slide → ffmpeg mux against the cached audio segments**, which reuses the §7 HTML renderer and the §8 TTS cache and adds mainly the slide template and the mux. The expensive version is animated diagram reveals and per-slide layout tuning.

If you build it, build the cheap one first and let Gate C (§10.3 — next-day unprompted recall vs the HTML doc) decide whether the expensive one is ever worth it.

---

## What this changes about the build

1. **Milestone 1 is smaller than it looks.** The parse engine, the git plumbing, the diagram renderer, and the cost accounting are all off-the-shelf. What's genuinely yours is the cross-file resolver, the archaeology chain, the research loop, and the doc schema — which is the right thing to be spending months on, because it's also the differentiation.
2. **Two hard requirements surfaced that the PRD doesn't mention:** authenticated forge access is mandatory (60/h unauth kills it), and cache affinity forbids OpenRouter's auto-router. Both belong in the config design.
3. **Three "obvious" choices are traps:** `tree-sitter-languages` (frozen), mermaid (needs a browser), and the LLM-eval frameworks (measure the wrong thing). All three are what you'd land on by default.
4. **MCP defers cleanly at zero cost** — `mcp` 2.x's backward compatibility means waiting is strictly free.

**Suggested first commit:** the symbol index + `sha256(node.sexp())` staleness hash, with `hypothesis` property tests over the resolver. It's the foundation for everything, it's the part nobody else has built, and it's testable without a single model call.
