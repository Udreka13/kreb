# kreb — PRD

**Status:** draft v0.6
**Owner:** Kuba
**Shape:** host-agnostic engine + thin adapters. Primary surface: MCP server. First native adapter: opencode.

---

## 1. Problem

AI-assisted development shifted the developer's job from active problem-solving to passive review. Throughput went up; the experience got worse. Three distinct costs, often conflated:

| Cost | Cause | What actually fixes it |
|---|---|---|
| Eye strain | Sustained backlit close-focus reading | Change the medium, or the time-of-day |
| Cognitive overload | Verifying invariants you didn't establish | Curation and narrative order |
| Loss of flow | Interrupt-driven review loop | Batching and asynchrony |

`kreb` targets **overload** primarily and **eye strain** secondarily. It does not claim to restore flow.

Secondary problem it solves as a side effect: institutional knowledge about *why* code is shaped the way it is exists only in git history, PR threads, and dead people's heads. Deriving it is tedious, so nobody does it, so it evaporates.

## 2. What it is

A codebase comprehension pipeline. One research pass produces a structured document; three renderers turn that document into artifacts suited to different attention budgets.

```
                    ┌─→ pretty doc (HTML)   — self-paced, at the desk
research doc (MD) ──┼─→ audio overview      — hands-free, eyes closed
                    └─→ video overview      — passive, diagram-led
```

The research doc is also a deliverable in its own right (`doc md`).

**Core design commitment:** all value lives in the research doc. Renderers are dumb transformations. If the doc is wrong, no format saves it; if the doc is good, adding a renderer is a weekend.

### 2.1 Name and mark

**kreb**, after Creb — the Mog-ur in *Clan of the Cave Bear*. One-eyed, crippled, useless on a hunt, and the most important member of the clan because he holds its memories and interprets them for people who cannot reach them directly. That is the job description in §1: institutional knowledge that lives in one place and evaporates when it goes. The crab reading is intentional and secondary — a pun you find rather than one that announces itself.

Registry position: `kreb` is free on both PyPI and npm. Published package `kreb`, CLI binary `kreb`, documented MCP config key `kreb`, so commands read `/mcp__kreb__audio`. Four characters at every surface the user touches.

**Mark:** a cave painting, not a mascot. Ochre and charcoal on stone, daubed rather than drawn — a one-eyed crab holding a club. The register matters: every other crustacean in this ecosystem (Ferris, OpenClaw) is a friendly rounded vector, and the prehistory framing is what makes this one legible as ours rather than as a trend. It also carries the joke the tool is built around — the developer as the caveman having things patiently explained to them by something more articulate.

Working palette: `#241C15` charcoal, `#C4622B` ochre, `#8E3419` oxide, `#E4D5B7` bone.

Open constraint from the first draft: the crab must read before the eye does. Cave art is frontal and symbolic, which pulls a centered eye into dominating the silhouette; the eye should be small enough that claws and legs establish the shape first. Must survive at 16px and on a dark background.

---

## 3. Architecture — engine and adapters

The pipeline has nothing host-specific in it. Binding it to one editor would be an accident of which tool got scratched first.

```
┌─────────────────────────────────────────────────────┐
│  ADAPTERS (thin)                                    │
│  MCP server · opencode · VS Code · CLI · CI         │
└───────────────────────┬─────────────────────────────┘
                        │  canonical request (§5.1)
┌───────────────────────┴─────────────────────────────┐
│  ENGINE (all the value)                             │
│   invocation parser  ·  research pipeline           │
│   doc store + cache  ·  symbol resolver             │
│   renderers: html · audio · video                   │
│   job manager  ·  budget accounting                 │
└───────────────────────┬─────────────────────────────┘
                        │  injected ports
        ┌───────────────┼───────────────┬─────────────┐
     inference        repo access      TTS      diagram/image
     provider        (fs + git)      provider     renderers
```

### 3.1 Split of responsibility

**Engine owns:** parsing free text into a request, deciding what's under-specified, gathering context, producing and validating the typed research doc, symbol resolution and staleness, caching, all three renderers, job lifecycle, budget accounting and enforcement.

**Adapter owns, and nothing else:** registering the invocation surface, passing free text through unmodified, relaying questions and progress in host-idiomatic form, and surfacing the finished artifact path. An adapter that contains prompt text or format knowledge is a bug — that logic drifted out of the engine.

### 3.2 Injected ports

- **Inference** — provider abstraction (§3.4).
- **Repo access** — fs + git, abstracted so CI and remote-repo use work without a local checkout.
- **TTS** — pluggable; local (Piper, Kokoro) or hosted. Local matters for cost and offline use.
- **Diagram/image rendering** — mermaid CLI, SVG, optional image model.

### 3.3 Distribution

- **Engine:** a library plus a headless CLI (`kreb doc md --repo . --json`). The CLI is the real integration boundary — stable JSON in, artifact paths out. Every adapter shells out to it or links it.
- **MCP server as the primary adapter.** One server reaches opencode, Claude Code, Cursor and VS Code with no per-host code. See §4.
- **Native plugins** only where a host-idiomatic surface is worth the extra code. opencode first, since it's the daily driver and the feedback loop matters more than reach early on.

Language choice is a genuine fork. The audio/video/tree-sitter toolchain is materially nicer in Python; opencode's plugin surface is TS. Recommendation: **engine in Python, adapters talk to it over the JSON CLI or MCP.** The subprocess boundary is cheap because the adapter surface is tiny. Distribute via `uvx` so install is one config line and no venv management leaks to users.

### 3.4 Inference: own key, own bill

**The "borrow the host's model access" path is closed.** MCP sampling (`sampling/createMessage`) was deprecated in protocol revision 2026-07-28 under SEP-2577 — it survives at least twelve months, but new implementations should not adopt it and existing ones should integrate directly with provider APIs. Independently, opencode's MCP client is constructed without sampling capability. Roots and logging were deprecated in the same SEP.

So kreb is **self-driving**: it runs its own research loop against its own provider credentials. Consequences, all of which have to be designed for rather than apologised for:

- Setup requires an API key. One, not several.
- The spend is invisible in the host's UI, so the budget controls in §6.6 are load-bearing, not decorative.
- Quality and doc-format guarantees are ours to enforce, which is the upside.

### 3.5 Providers and model roles

**v1: OpenRouter, single provider.** One key, one account, access to every model family, and — the real reason — per-generation cost accounting comes back in the response, which is what makes §6.6 honest rather than estimated. Provider fallback and routing come free.

The port is abstracted from day one so direct Anthropic/OpenAI/Google, local (Ollama, llama.cpp), and gateway providers can land later without touching the pipeline. Not v1.

Models are configured **by role, not globally.** A single model choice is wrong: the research pass needs long-context reasoning and tool use, script generation is a rewrite task, slide copy is nearly mechanical.

| Role | Work | Needs |
|---|---|---|
| `research` | Context gathering, blame/PR archaeology, synthesis into typed sections | Frontier reasoning, long context, strong tool use. The one place to spend. |
| `narrate` | Audio and video script generation from the doc | Mid-tier. Prose quality over reasoning. |
| `visualize` | Diagram layout, slide structure | Mid-tier, reliable structured output. |
| `mechanical` | Summaries, titles, section labels, parse of free text | Cheap and fast. |

Ships with three profiles — `budget`, `balanced` (default), `max` — each a role→model map, overridable per role in config. **Pin exact model IDs at release rather than in this document**; OpenRouter identifiers move, and a stale default is worse than none. Rule of thumb: frontier model for `research`, mid-tier for `narrate`/`visualize`, cheapest competent for `mechanical`.

---

## 4. MCP surface

### 4.1 Prompts first, tools second

Agent-determined invocation is unreliable for exactly this use case. Asked to "explain the auth module," an agent will usually just read the code and explain it — it's already good at that, and it's one step instead of five. A tool description competes against the model's default behaviour and loses on the cheap cases.

So:

- **Prompts are the primary surface.** MCP prompts appear as real slash commands (`/mcp__xplain__audio`) in Claude Code and VS Code. Deterministic: the user asked, so it runs. Namespacing is ugly; determinism is worth it.
- **Tools are the secondary surface**, for genuine mid-task invocation: "I've finished this refactor, generate the onboarding doc."
- Where a host supports neither well, the CLI is the fallback and loses nothing.

### 4.2 Under-specified requests

The tool/prompt description tells the calling agent what kreb does *and* how to behave when the request is thin. But the decision itself belongs in the engine, not in the agent's judgement.

Flow: engine parses free text → classifies each parameter as `given` / `defaulted` / `must-ask` → either proceeds or comes back for input.

- **MCP elicitation** is the mechanism where supported (not deprecated by SEP-2577; still current). Structured question, host renders it, answer returns.
- **Fallback for clients without elicitation:** the call returns `{status: "needs_input", questions: [...], defaults_if_skipped: {...}}`. The description instructs the agent to put these to the user and re-call with the answers, or to accept the stated defaults if the user says "just do it."

Ask sparingly. Friction is the thing we're trying to remove, and a tool that interrogates gets abandoned.

| Parameter | Default | Ask when |
|---|---|---|
| `mode` | — | Always required; inferable from the prompt/command used |
| `scope` | Whole repo | Repo is large enough that whole-repo research would exceed the cost threshold |
| `depth` | 2 (architecture) | Never — steerable after the fact via re-render |
| `length` | Derived from depth | Never |
| `budget` | Config ceiling | Preflight estimate exceeds the warn threshold |
| voices / images | Off | Never — config only |

The preflight estimate (§6.6) is the natural and usually only elicitation point: *"Whole-repo research at depth 2, est. $X. Narrow to `src/auth`, proceed, or set a lower depth?"* One question, real information, obvious answers.

### 4.3 Async execution and the return contract

A research pass runs for minutes. A blocking call that long hits timeouts and freezes the host UI.

- Use the `io.modelcontextprotocol/tasks` extension (moved out of experimental in the 2026-07-28 revision, poll-based `tasks/get` plus `tasks/update`). Start returns a job id immediately; progress is polled.
- **Return paths, never content.** Dumping a 20k-token HTML doc into the agent's context defeats the entire purpose of the tool.
- Return to both audiences in one payload: a human-readable path/URI for the user, and the machine path plus a one-paragraph abstract and the section index for the agent — enough for it to decide whether to open the doc when answering follow-ups, without loading it speculatively.
- Agent guidance in the description: reading the MD or HTML on demand is encouraged; audio and video are for the human only.
- Jobs survive host disconnect. A run started in one session is retrievable in the next — this is also what makes the "start it before lunch" pattern work.

---

## 5. Interface

### 5.1 Canonical request

Every adapter normalises to the same thing:

```
kreb <mode> <freetext>
```

`mode` ∈ `doc md` | `doc pretty` | `audio` | `video`. Everything after is free text, parsed by the engine into a research brief and render options.

```
/kreb audio, I want to learn about the domain
/kreb doc pretty — how does auth work, deep
/kreb video the ingestion pipeline, 5 min
/kreb doc md why is the retry logic like this
```

Parsed into: scope, question type, depth, length target, output prefs. When the parse is ambiguous but harmless, state the interpretation in one line and proceed. When it's ambiguous and expensive, ask (§4.2).

**Free text is passed through verbatim.** Adapters must not pre-parse, pre-structure, or reformat it; parsing quality is an engine concern and must improve in one place.

### 5.2 Question types (routing hint)

- **"what"** — structure, API surface. Cheap. Reading beats listening; steer toward `doc`.
- **"why"** — rationale archaeology across blame, PRs, issues, dead code. Expensive to derive, narrative in shape, tolerant of imprecision. **This is the killer use case** and the best fit for audio.
- **"how"** — flows, lifecycles, sequences. Best fit for video/diagrams.

---

## 6. Stage 1 — Research doc

### 6.1 Research execution and repo scale

**Two drivers, user-selectable in config (`research.driver`).** Rendering is always engine-side; only the research loop is pluggable.

| | `engine` (default) | `host` |
|---|---|---|
| Who researches | kreb's own agent loop, own key | The calling agent, its tools, its credits |
| Doc format guarantees | Enforced by construction | Validated on submission; may need retries |
| Runs headless / in CI | ✓ | ✗ — blocks on a live session |
| Async, survives disconnect | ✓ | ✗ |
| Host context cost | Zero (paths only) | Fills the user's context with research output |
| Second API key | Required | Not required |

Host-driven mode does not need sampling (deprecated, §3.4). Mechanism: the tool returns a research brief plus the section schema, the agent works with its own navigation tools, and posts sections back via `kreb_submit_section` for validation and rendering. Multi-turn falls out of repeated tool calls.

Default to `engine`. Offer `host` for users who won't add a second key, and accept that async, CI, and context-hygiene are lost in that mode.

**Library choice for the engine loop:** the pattern needed is planning + sub-agents + a scratch filesystem — the deep-research loop. `langchain-ai/deepagents` implements it. Evaluate, but weigh dependency weight and framework lock-in against the fact that this loop is a few hundred lines when written directly. Decide after the first spike, not before.

#### Retrieval: navigate, don't embed

**Do not embed the codebase.** Reasons, in order:

- Embeddings retrieve by surface similarity; comprehension needs causal and structural traversal. "Why is the retry logic like this" is answered by a call site three modules away, a PR thread, and a reverted commit — none of which are semantically similar to the query.
- The strongest signal is already structured and free: imports, call graph, type hierarchy, module boundaries. A tree-sitter symbol index gives exact answers where embeddings give fuzzy ones — and §6.3 requires that index anyway, so it is not extra work.
- Embeddings add a second staleness domain on top of the symbol-based one, plus a vector store, an embedding model, and a re-index pipeline.
- Coding agents converged away from RAG-over-code toward agentic navigation. That convergence is evidence.

**Where embeddings would earn their keep, and only there:** the prose corpus — commit messages, PR descriptions, issue threads, ADRs, design docs. That is natural language, it is exactly the "why" archaeology corpus, and lexical search over it is genuinely weak. Optional, deferred, and clearly scoped as *prose only*.

Narrow exception for code: entry-point selection on a very large repo when naming doesn't cooperate (the auth module is called `gatekeeper`). Handle with the module summaries below before reaching for vectors.

#### Scale strategy

The 1M-token context windows now available do not make this go away — a mid-size monorepo still exceeds them, and stuffing context is both expensive and worse than selection.

1. **Deterministic index**, built once and updated incrementally: symbol table, import graph, module tree, file sizes and churn. Cheap, exact, no model calls.
2. **Progressive summarization, leaf to root.** Summarize each module from its symbols and its children's summaries. Produces a compressed map of the whole repo at bounded cost, and gives the research agent something to navigate rather than a file list. This is the map-reduce answer to scale.
3. **Agent drills down** from the map into specific symbols, callers, and history — never a whole-repo dump.
4. **Exploit prompt caching.** The repo map and conventions form a stable prefix reused across every section-level call. At current provider cache discounts (up to ~100x on some models) this dominates the cost model, so structure calls to preserve prefix stability rather than to minimise call count.

Module summaries are cached and staleness-tracked exactly like doc sections (§6.3).

#### External sources: dependencies and domain

Code does not explain itself. Two gaps are unfillable from the repo alone: **domain jargon** (a claims-processing codebase says `subrogation` and `IBNR` and never defines them) and **library semantics** (what the framework does by default, which the code only overrides). Both are genuine comprehension blockers, so external research is in scope — but under a hard rule.

**Hard rule: external sources may explain concepts. They may never assert what this codebase does.** Any claim about repo behaviour must carry a symbol anchor. External evidence supports background only.

This exists because the failure mode is specific and convincing: the agent reads a library's documentation, then describes the library's behaviour as though it were this repo's. The doc reads as well-researched and is wrong precisely where the repo is interesting — at the override. It is the paraphrase-the-code failure wearing a lab coat.

Inverted, the same pairing is the highest-value output the tool can produce: *"the library retries with exponential backoff by default; this codebase disables that and retries linearly, per `src/http/retry.py#RetryPolicy` and PR#412."* Neither source yields that alone. This is the shape of a Gate B novel-true statement.

**Two modes, and they are not the same feature.**

| | `deps` | `domain` |
|---|---|---|
| Question | "What does this library do by default?" | "What does this word mean in this industry?" |
| Source | Official docs for the **pinned** version, resolved from the lockfile | Open web search |
| Discovery | Lockfile → package metadata → homepage/repo URL. No search engine needed. | Search API (Exa, Tavily, Brave) |
| Risk | Low; allowlisted by construction | Moderate; open corpus, injection surface |
| Output | Behavioural baseline to diff the code against | Glossary entries, `depth: 1` framing |

Read the lockfile first and **scope every dependency lookup to the pinned version.** Latest-version docs describing a version the repo doesn't use is the largest source of wrong-but-plausible external claims.

Config: `research.web = off | deps | full`. Default `deps` — most of the value, most of it allowlisted.

**Constraints:**

- Domain research produces glossary and framing sections. It is capped: a fixed budget slice, not an open-ended crawl.
- Fetched content is untrusted input. Treat it as data, never as instruction, and never let it reach a tool-invoking context unfiltered.
- External evidence is cached in `.kreb/external/` with a TTL, and invalidated on lockfile change. It does not participate in symbol-based staleness — different domain, different trigger.
- In `host` research mode the calling agent usually has its own web search; the same rule applies to submitted sections and is enforced at validation.

#### Language scope

Symbol anchoring is per-language tree-sitter work and is the main driver of effort.

- **v1:** Python and TypeScript/JavaScript.
- **Degraded mode for everything else:** file- and path-level anchors instead of symbol-level, staleness at file granularity. Usable, visibly weaker, and honest about which it is.
- Adding a language is a grammar plus a symbol-extraction query — bounded, and a good first external contribution.

### 6.2 Format

Markdown with typed section front-matter. Structure is explicit so renderers select rather than re-derive.

```yaml
- id: retry-policy
  title: Retry and backoff
  depth: 2                # 1=exec summary, 2=architecture, 3=implementation
  kind: rationale         # structure | rationale | flow | gotcha
  evidence:
    - src/http/retry.py#RetryPolicy      # local, symbol-anchored
    - PR#412
    - commit 8d9e1
    - ext:https://docs.example/retry@v2.3   # external, background only
  confidence: derived     # verified | derived | speculative | background
  parent: http-client
```

- `verified` — read directly off code/AST.
- `derived` — synthesised from multiple sources, defensible.
- `speculative` — plausible inference, explicitly flagged. Survives into every renderer with a visible tell.
- `background` — sourced externally (docs, domain references). Explains a concept; asserts nothing about this repo. A section whose only evidence is `ext:` may not make a behavioural claim — enforced at validation, not by prompt.

### 6.3 Citation anchors

Anchor to **symbols, not line numbers**: `path#SymbolName`, resolved via tree-sitter at render time; record the commit SHA for permalinks. Line refs rot on the next commit.

This also yields staleness for free: a section declares the symbols it depends on; if those AST subtrees changed, mark the section stale. Cached artifacts become self-maintaining ADRs.

### 6.4 Depth is a query, not a prompt instruction

`--depth 2` selects a subtree of sections. `--focus retry-policy` selects a subgraph. Asking the model to "be brief" yields compressed mush; selecting sections yields a coherent shorter document. Re-rendering at a different depth costs nothing and hits cache.

### 6.5 Cache and version control

`.kreb/` holds research docs, scripts, rendered audio segments, diagrams. Keyed on content hash + symbol dependency set.

**Gitignored by default; committing is a supported choice.** `kreb init` appends `.kreb/` to `.gitignore` and says so out loud. Config flips it.

| | Committed | Ignored (default) |
|---|---|---|
| Team gets docs without paying to regenerate | ✓ | ✗ |
| Onboarding artifacts reviewable in PR | ✓ | ✗ |
| Merge conflicts in generated MD/binaries | ✗ | ✓ |
| Repo weight (audio/video) | ✗ | ✓ |

If committed, split it: `.kreb/docs/` (text, commit-friendly) tracked, `.kreb/media/` (audio, video, rasters) ignored regardless. Media is reproducible from scripts; text is the thing worth reviewing.

**Config lives at repo root (`kreb.toml`), not inside `.kreb/`** — otherwise the default ignore rule swallows the user's settings.

### 6.6 Budget

Credit-consuming by nature, and — per §3.4 — spent outside the host's visibility. **No default cap**; the engine does not silently truncate research to hit a number nobody chose.

- User sets the ceiling: `budget.max_per_run`, optional `budget.max_per_day`, in currency (OpenRouter reports actual cost per generation, so this is measured rather than guessed from token counts).
- **Preflight estimate** before any spend, from scope size and depth. Shown with the option to narrow — usually the only question kreb asks (§4.2).
- Live accounting during the run; warn at a configurable threshold.
- Hitting the ceiling **stops cleanly and persists partial work.** Completed sections are written and valid; the run is resumable. A killed run that produced nothing is the worst outcome.
- Renderers accounted separately from research: users will want different ceilings for "think hard" and "read it aloud."
- Cache hits are free and reported as such, so a re-run's near-zero cost is visible.

---

## 7. Stage 2a — Pretty doc (HTML)

Self-paced comprehension at the desk. Segmenting principle: learner-controlled pacing beats fixed pacing, and HTML is the only renderer that grants it.

- **Click-to-expand code, silent.** Signature inline, body one tap away. Never narrated. The value over reading the repo is *curation* — 30 lines out of 3000, in dependency order — not the display surface.
- Diagrams inline, next to the prose that motivates them.
- Confidence markers rendered visually; speculative claims are visibly different.
- Per-section audio playable inline (shares §8's TTS cache) — optional, off by default.
- Single self-contained file. Diffs in git. Opens on a phone. Renders inline in hosts that allow it, opens in a browser where they don't.

Ship this renderer first: it is the only one where a bad research doc is immediately obvious.

## 8. Stage 2b — Audio overview

Speech is a bandwidth downgrade (~150 wpm, ~250 sped up). It wins in exactly two situations: the time was already dead (running, cycling, commuting), or the visual channel is spent and throughput is being traded for recovery. Scope accordingly.

Good for: rationale, orientation before a work session, domain/architecture at depth 1–2.
Bad for: review, debugging, anything needing jump-or-verify.

### 8.1 Script generation

Distinct from the video script. Self-contained — carries structure verbally, no visual crutches.

**Deep questions, not two voices.** Vicarious-learning research (Driscoll/Craig/Gholson et al.) held tutor wording constant across monologue and dialogue conditions: the two-voice format alone produced no significant gain. What produced significantly better recall was the *tutee asking deep questions*. Follow-up work found the number of distinct perspectives mattered, not the formal voice count. Overhearing question-driven discourse also increased learners' own subsequent deep questioning; monologue increased shallow questioning.

Therefore:

- `--questions deep` is the primary quality lever, on by default. One narrator posing and answering hard questions satisfies it.
- `--voices 2` is a preset, not a feature. Justified where a skeptic voice is natural — tradeoffs, rejected alternatives, decisions with real losers.
- Cut NotebookLM's enthusiasm padding entirely. Roughly 40% of that format is filler.

### 8.2 Constraints

- Steerable length via section selection (§6.4), then a per-section word budget.
- Never read code aloud beyond a symbol name.
- Speculative claims get a verbal hedge, not just a doc-level marker.
- TTS cached per section, keyed on script hash — a one-paragraph edit re-renders eight seconds, not forty minutes.
- Transcript always emitted alongside audio. Hallucinated audio is worse than hallucinated text: it sounds authoritative and can't be cross-checked mid-run. The transcript is what makes it trustworthy and what's searchable later.

### 8.3 Interruption (stretch, high value)

"Wait, go back to the retry logic" mid-listen. This is the single feature that converts passive listening into the deep-question mode the research endorses — the learner asking, rather than overhearing.

### 8.4 Capture path (required, not optional)

Hearing "the token refresh is racy" at km 20 with no way to record it is its own strain. Voice memo → transcribed → appended to `.kreb/notes/`, surfaced next session.

## 9. Stage 2c — Video overview

Voiceover + slides. Does **not** consume the audio artifact — it needs its own script.

### 9.1 The redundancy constraint (load-bearing)

Mayer's redundancy principle: people learn better from graphics + narration than from graphics + narration + on-screen text. Reading and viewing compete for the same channel, and reconciling two verbal streams itself costs working memory.

**Code is text.** So the obvious build — screencast of a file with a voice explaining it — is precisely the failure case, and it's the thing that gets built first by default. Rules:

- Never narrate over code the viewer is expected to read.
- Diagram + narration, **or** code on screen + silence with the relevant lines highlighted. Not both.
- Narration *complements* the slide; it does not read it. (Contrast with §8.1, where the script must be self-contained.)
- Exception, narrow and evidenced: 2–3 word labels placed inside a diagram next to the element they name improved retention (Mayer & Johnson). Annotate diagrams; don't subtitle them.
- Pre-training: name the four types before narrating the flow through them.

### 9.2 Slides

Generated **from the research doc's section tree**, not invented by the model as a deck. Keeps video in sync when the doc changes; makes staleness propagate.

### 9.3 Diagrams — the axis that matters is provenance

| | Source | Correctness | Treatment |
|---|---|---|---|
| **Extracted** | AST traversal (import graph, class hierarchy, call paths); model does layout only | Correct by construction | Default. No caveat needed. |
| **Asserted** | Model's belief about a flow or lifecycle | May be wrong | Visual tell, same as speculative claims |

A confident box-and-arrow diagram is the artifact most likely to ossify a wrong mental model — this distinction matters more than the mermaid-vs-image question.

**Image generation:** opt-in flag, agent's discretion within it. Legitimate for occasional conceptual illustration (metaphors, non-structural concepts). Never for anything representing actual code structure — that's what extraction is for. Decorative visuals are extraneous load (coherence principle) and are a bug, not a feature.

Renderer priority: extracted mermaid → prettified JS/SVG → generated image (if enabled).

---

## 10. Success criteria

The PRD's central claim is that all value lives in the research doc. That is an empirical claim, and nothing below stage 1 should be built until it holds. Four gates, in order.

### 10.1 Gate A — Factuality (mechanical, automatable)

This is the regression suite. Run on every change to the research pipeline.

| Check | Threshold |
|---|---|
| `verified` claims whose symbol anchor resolves in the AST | 100%. A dangling anchor is a hard fail, not a warning. |
| Claims citing symbols that do not exist anywhere in the repo | 0. Fabricated anchors are the worst failure mode — they look like evidence. |
| Sampled `derived` claims defensible on manual audit (n=20 per repo) | ≥90% |
| Claims typed `verified` that are actually inferred | ≤5%. Confidence inflation destroys the whole trust model. |
| Correct sections flagged stale after a synthetic refactor | 100% |
| Behavioural claims about the repo whose only evidence is `ext:` | 0. Hard fail — this is library docs impersonating codebase knowledge. |
| Dependency claims checked against the **pinned** version rather than latest | 100% |

Build a small corpus of 3–5 repos (one you wrote, one mid-size OSS, one large monorepo, one non-v1 language) and keep the audit set with it.

### 10.2 Gate B — Usefulness (human, the one that matters)

**Known-repo test.** Run on a codebase you know cold. Count two things: true statements you did not already know, and confidently wrong statements. Target: **≥3 novel-true, 0 confidently-wrong at `verified` confidence.** If it cannot beat you on a repo you know, it will not help on one you don't — you just won't be able to tell.

**Unknown-repo test.** Pick an OSS project you have never touched with a good first issue. Read only the doc, then attempt the fix. Compare against a control run with no doc. The measure is time to first correct patch, not subjective comprehension.

**Archaeology test.** Ask a "why" question whose answer is genuinely buried — a decision reversed in history, a workaround for an upstream bug. Does the doc find it, or does it paraphrase the code? Paraphrasing the code is the failure mode this whole product exists to avoid, and it is the one that will look most convincing.

### 10.3 Gate C — Per-renderer

Each renderer earns its existence separately.

- **HTML:** do readers expand the code blocks? If never, curation failed and the doc is prose about code rather than a guide into it.
- **Audio:** completion rate. Abandonment at three minutes means the script is padding, not that audio was the wrong idea.
- **Video:** unprompted recall of the named concepts a day later. If it does not beat the HTML doc on recall, it is not worth the render cost.

### 10.4 Gate D — Economics

Cost per doc has to sit below the annoyance threshold, or the tool becomes something people admire and never run. Working target: **under $2 for a depth-2 doc on a mid-size repo**, re-renders near zero on cache hits. If the honest number is $20, the product is a novelty regardless of quality.

### 10.5 Anti-metrics

Do not optimise for: document length, section count, "coverage" of files, or number of diagrams. Every one of these is trivially gameable by generating more, and generating more is the disease.

---

## 11. Non-goals

- Not an editor. Read-and-instruct only; execution stays on the dev machine.
- Not a replacement for reading code during review or debugging.
- Not a real-time assistant — the latency is acceptable, arguably desirable.
- Not tied to one host. No engine feature may require a specific editor.
- Not multi-provider in v1. The port exists; the implementations don't.
- Not all languages in v1. Python and TS/JS get symbol-level anchoring; everything else runs degraded (§6.1).
- No handwriting OCR (see §13).
- No claim to increase shipping velocity.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Wrong claim laundered through three formats, arriving authoritative where it can't be checked | Typed evidence, confidence markers surfaced in every renderer, transcript always emitted |
| Doc rots against the codebase | Symbol-anchored dependencies + staleness marking (§6.3) |
| Agent never invokes the tool because it can answer directly | Prompts/slash commands as primary surface (§4.1); tool description scoped to what the agent genuinely can't do |
| Surprise bill on an invisible second account | Preflight estimate, measured cost from OpenRouter, hard ceiling, resumable partial runs (§6.6) |
| Elicitation turns into interrogation; users stop invoking it | Ask at most once, only on cost; everything else defaults (§4.2) |
| Logic leaks into adapters; behaviour diverges per host | Adapters carry no prompts and no format knowledge; one CLI contract test suite run against every adapter |
| Audio produced for "what" questions nobody wants to listen to | Route by question type (§5.2); resist the demo-friendly use case |
| Video built as narrated code screencast | §9.1 is a hard constraint, not a guideline |
| Source code egresses to a third-party provider — a hard blocker in regulated or IP-sensitive orgs | Acknowledged constraint, not solved in v1. Local-model providers and `host` research driver (§6.1) are the mitigations; state it plainly in positioning rather than discovering it in a sales conversation |
| Doc paraphrases the code instead of explaining it, and reads convincingly while doing so | Gate B archaeology test (§10.2) exists specifically to catch this |
| Library docs get restated as this repo's behaviour — well-researched and wrong at exactly the interesting point | Hard rule in §6.1: behavioural claims require a symbol anchor; enforced in Gate A, not by prompting |
| Fetched web content carries injection payloads into a tool-using agent | External content is data, never instruction; `deps` mode is allowlisted from the lockfile; `full` is opt-in |
| Sampling deprecation window closes and something else follows | Inference stays behind a port; nothing in the pipeline knows about MCP |

## 13. Adjacent, deferred

**E-ink review client.** Same engine, EPUB renderer — a fourth sibling on the fan-out, not a new product. The real value is not the display: a device physically incapable of fast round-trips forces batching, buying asynchrony. Eye comfort is a bonus. EPUB export is ~50 lines once the doc exists (zip + OPF + nav). Return path for annotations: export the annotated page as an image and hand it to a VLM — margin notes are prose, not syntax, so this works today without building OCR. Test jump-to-definition friction early; if review style needs forty jumps a page, e-ink fails hard.

**CI adapter.** Regenerate stale sections on merge to main; open a PR with the diff. Nearly free once repo access is a port and the CLI is headless.

**Additional providers.** Direct Anthropic/OpenAI/Google for users who already have keys; local models for the `mechanical` role, where quality demands are lowest and volume is highest.

## 14. Dev sequencing

Distinct from the runtime DAG. Each stage ships only if the previous one is actually good.

1. **Index + research loop** — tree-sitter symbol index, module summarization, engine-driven research, typed doc format, symbol anchors, cache, OpenRouter provider, role/profile config, budget accounting. Driven by the headless CLI from the first commit; no host in sight.
2. **Gate A harness** — build it alongside stage 1, not after. It is cheap now and impossible to retrofit honestly.
3. **Gate B on a known repo.** Stop here if it fails. Everything downstream multiplies stage 1's quality, including its errors.
4. **HTML renderer** — the second honesty check on stage 1.
5. **MCP server** — prompts, tools, elicitation, tasks-based async, path-only returns. Host research driver lands here.
6. **Audio** — script profile, TTS port, caching, transcript.
7. **Capture path** (voice memo in).
8. **Video** — slides from section tree, extracted diagrams, mux.
9. opencode native plugin; interruption mode; EPUB; CI adapter; prose embeddings if archaeology is still weak.
