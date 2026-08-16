# kreb — architecture draft v0.1

**Status:** proposal, for adversarial review
**Constraints taken as given:** open source, built for the author's own use, no monetization. All three renderers (HTML, audio, video) are in scope and must be modular enough that they cannot destabilize the core. Python engine, `uvx` distribution.

---

## 1. The organizing idea: a content-addressed artifact DAG

Everything kreb produces is a **node in a DAG of derived artifacts**, each keyed on a content hash of its inputs plus the parameters that produced it. Nothing is a "step in a script"; everything is a cached transform with a stable identity.

```
repo@sha ──▶ index ──▶ map ──▶ doc ──┬──▶ html
                                     │
                                     ├──▶ narration ──▶ audio
                                     │
                                     └──▶ storyboard ──▶ video
```

Every edge is a pure function of `(input artifact hash, params, model role config)`. That gives three properties for free:

- **Caching and resumability** — a run is "walk the DAG, materialize missing nodes." Interrupting and resuming is the same code path as a cache hit.
- **Staleness propagation** — when a symbol's AST subtree hash changes, the sections depending on it go stale, and staleness flows downstream to the narration segment, the audio clip, and the video scene that derive from them.
- **Renderer isolation** — a renderer is a function from one artifact type to another. It cannot reach back into the research loop, because it has no reference to it.

**This is the answer to "will audio/video destabilize the core."** They are leaf subtrees. Deleting `kreb/render/video/` entirely leaves a working product; adding a fourth renderer (EPUB, §13) touches nothing upstream.

## 2. Where I think the PRD is wrong, and what I propose instead

§35 says *"all value lives in the research doc; renderers are dumb transformations."* The first clause is right and load-bearing. The second is false for two of the three renderers, and pretending otherwise is what makes video eat two months.

The honest classification:

| Renderer | Transform | Model calls |
|---|---|---|
| HTML | **projection** — genuinely dumb; selects and formats what's already there | 0 |
| Audio | **rewrite** — prose→speech needs different sentence shapes, verbal structure, hedges | 1 pass |
| Video | **re-authoring** — needs slide breaks, diagram placement, pacing, on-screen/narration split | 2 passes |

So I propose **naming the intermediate artifacts and making them first-class**, rather than hiding a second authoring pass inside a "renderer":

- **`narration`** — the audio script. A typed artifact: ordered segments, each carrying `source_section`, text, and hedges for speculative claims. Cached, hashed, staleness-tracked, diffable. §8's per-section TTS cache keys off *this*, not the doc.
- **`storyboard`** — the video plan. Ordered scenes, each with: narration text, a visual (diagram ref, code excerpt, or title card), a `visual_mode` of `narrate` or `silent`, and a duration derived from the narration.

The redundancy constraint from §9.1 then becomes a **validator on the storyboard**, not a prompt instruction: a scene with `visual = code` and `visual_mode = narrate` is a **hard validation error**. That is the difference between a rule that holds and a rule that a model drifts away from at 2am.

Restated principle: **all value lives in the research doc; every derived artifact is typed, cached, and validated.** The doc is still the only place facts enter the system — narration and storyboard may *reformat and select*, never *assert*. A narration segment cites the section it came from; a validator checks that it introduces no new symbol references.

## 3. Module layout

```
kreb/
  index/        tree-sitter symbol index, import graph, subtree hashes
  archaeology/  git + forge evidence chains
  research/     planner, section writers, the agent loop
  doc/          typed schema, validation, store
  provider/     OpenRouter port, cost accounting, caching discipline
  jobs/         SQLite registry, resumption
  render/
    html/       projection
    narration/  doc → narration  (shared by audio + video)
    audio/      narration → wav/mp3 via TTS port
    storyboard/ doc + narration → storyboard
    video/      storyboard → mp4 via ffmpeg
  cli/
  mcp/          deferred; thin wrapper over the same library
```

**Dependency rule, enforced by a test:** `render/*` may import `doc/` and `provider/`, never `research/`, `archaeology/` or `index/`. `index/` imports nothing of kreb's. A cycle check runs in CI.

Note `narration/` is shared. Video's script derives from the same narration artifact as audio, differing in the storyboard's selection and pacing — §9's "video does not consume the audio artifact" holds at the *audio file* level, but the script layer is genuinely common and duplicating it would mean two prose-quality problems to solve instead of one.

## 4. The core data model

```python
class Evidence(BaseModel):
    kind: Literal["symbol", "commit", "pr", "issue", "external"]
    ref: str                      # path#Symbol | sha | PR#412 | url
    commit_sha: str | None        # permalink anchor

class Section(BaseModel):
    id: str
    title: str
    depth: Literal[1, 2, 3]
    kind: Literal["structure", "rationale", "flow", "gotcha"]
    confidence: Literal["verified", "derived", "speculative", "background"]
    evidence: list[Evidence]
    depends_on: list[SymbolRef]   # drives staleness
    parent: str | None
    body: str
```

**Validation is structural, not advisory.** Three rules run at write time and fail the section:

1. `confidence == "verified"` ⟹ every symbol in `evidence` resolves in the AST.
2. A section whose evidence is exclusively `kind == "external"` may not make a behavioural claim about the repo (§6.2's rule, enforced by construction: such sections are forced to `confidence = "background"` and a linter rejects repo-scoped verbs in them).
3. Any cited symbol not present in the index ⟹ hard fail, fabricated anchor.

These are Gate A checks running *inline*, not just in the test suite. The suite then re-runs them over a corpus.

## 5. The pieces I am least sure about

Flagging these because they're where an adversarial reviewer should start:

1. **The cross-file resolver is the whole build.** Wave 2 concluded nothing off-the-shelf does symbol index + re-export resolution + subtree staleness. That's ~1–2k LOC of the trickiest code, it's on the critical path for *everything*, and Python re-exports and TS barrel files are genuinely nasty. If this is wrong, nothing downstream is trustworthy.
2. ~~**Staleness granularity.** Hashing `node.sexp()` marks a section stale too eagerly.~~ **RESOLVED BY EXPERIMENT — and the original design was broken in the opposite direction.** See §7.
3. **Whether the research loop should be one agent or a planner + section-writers.** The latter parallelizes and keeps context small, but sections then can't see each other and the doc reads like eight disconnected essays.
4. **Video timing.** Durations come from TTS output, so the storyboard can't know scene lengths until after audio renders — meaning the DAG edge `storyboard → video` actually needs audio as an input too. That may break the clean layering above.
5. **Cost of the archaeology pass.** Every symbol worth explaining triggers blame + pickaxe + a forge lookup. On a mid-size repo that could be hundreds of forge calls against a 5,000/h budget, and the token cost of reading PR threads is unbounded.

## 6. Build order

1. `index/` + subtree hashing + property tests. No model calls. Testable in isolation.
2. `archaeology/` + evidence chains. Still no model calls; `git` and `gh` are deterministic.
3. `doc/` schema + validators + Gate A harness.
4. `research/` loop. The first place a model appears.
5. `render/html/`.
6. **Gate B on a known repo. Stop here if it fails.**
7. `render/narration/` + `render/audio/`.
8. `render/storyboard/` + `render/video/`.
9. `mcp/`.

Steps 1–3 are ~half the risk and involve no LLM nondeterminism at all.

---

## 7. Staleness: measured, not assumed

The tooling survey recommended `sha256(node.sexp())` as the staleness hash. **I tested it against real tree-sitter 0.26 and it is wrong — dangerously so.** The S-expression carries node *types and field names only*, never token text:

```
(function_definition name: (identifier) parameters: (parameters (identifier) (identifier))
  body: (block (assignment left: (identifier) right: (integer))
               (return_statement (comparison_operator (identifier) (identifier)))))
```

`(integer)` is identical whether the literal is `3` or `5`. `(comparison_operator ...)` is identical for `<` and `<=`.

Measured results, hashing the `should_retry` function under each edit:

| Change to the code | structural (`sexp`) | token-normalized |
|---|---|---|
| formatting only (`retries  =  3`) | unchanged ✓ | unchanged ✓ |
| comment added above the line | **CHANGED ✗** (false positive) | unchanged ✓ |
| `retries = 3` → `retries = 5` | **unchanged ✗ (silent miss)** | **CHANGED ✓** |
| `attempt < retries` → `<=` | **unchanged ✗ (silent miss)** | **CHANGED ✓** |
| variable renamed | unchanged ✗ | CHANGED ✓ |
| statement added | changed ✓ | changed ✓ |

The structural hash is **simultaneously too insensitive and too sensitive**: it silently misses the retry count changing from 3 to 5 — precisely the kind of claim the doc makes — while firing on an added comment. It is strictly worse than the alternative.

**Use a token-normalized hash instead:** walk the subtree, collect the source text of every *leaf* node, skip `comment` nodes, join with a separator, hash that.

```python
def token_hash(node, src, skip=("comment",)):
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

This ignores whitespace, formatting and comments while catching every literal, operator, and identifier change. It is the correct granularity, and it is about as cheap as the broken version.

**Two API corrections while I was in there:**

- `Node.sexp()` **does not exist in tree-sitter 0.26** — it was removed. Use `str(node)`. Any code written from the survey's recommendation would not have run.
- The PyPI distribution is `tree-sitter`, not `py-tree-sitter`.

**One open knob:** a change to a function's *own* docstring is detected by the token hash (verified). Whether that should mark a section stale is a judgement call — add `string` to `skip` for the docstring position if not. I would leave it detecting, since the doc often quotes docstrings.
