# kreb — build plan and status

Living document. Updated as work lands. The architecture it implements is
[`idea/docs/architecture.md`](idea/docs/architecture.md); this file tracks *where we are*
against it, not what it says.

**Last updated:** 2026-08-16 · 369 tests green · **kreb runs end to end**

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
| 8 | `research/` outline + writers + stitch | **done** | section is the DAG unit |
| 9 | `viz/` + `render/html` + `render/markdown` | **done** | zero-JS HTML; d2 degrades |
| 10 | `cli/` | **done** | argparse; runs keyless except `doc` |
| 11 | `progress/` event stream | **done** | 22 tests; stderr only, MCP-shaped |
| 12 | `doc/gate_b` + worksheet | **done** | 23 tests; builds the sheet, scores nothing |
| — | **Gate B** | **next** | **stop-or-continue point** — needs a real API key |
| 13 | `render/beats` + `render/narration` + `render/dialogue` + `render/critique` + `tts/` + audio | **done** | 154 tests; presets, two-host, judged not gated |
| 14 | `render/storyboard` + video | pending | consumes `beats` + `timings` |
| 15 | `mcp/` | pending | deferred deliberately |

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

### Round 4 (hermes, on `budget/`, `provider/`, `doc/`, `forge.py`)

Two reviewers on an isolated copy of the repo. Seven real defects, every one
reproduced by running code before it was fixed, every fix mutation-verified.

Spend accounting — the number a ceiling is enforced against:

1. **A validator that *raised* lost the charge.** The completion existed and had
   been billed, but the ledger write sat after the validate call, so a validator
   bug read as free inference. Charging now happens in a `finally`.
2. **Two `Ledger`s on one file each under-counted the other.** Research and
   render are metered separately but share a path; each held its
   construction-time snapshot, so a $10 daily ceiling let two phases spend $8
   apiece and neither stopped. Totals now resync the appended tail.
3. **Phase ceilings were unenforced without a `phase` argument** — configured,
   reported as configured, never applied.
4. **`warn_at` watched only the run ceiling**, so a day-only budget hit its cap
   with no warning.

Validation — the rules that decide whether a document may claim to be factual:

5. **A symbol named only inside a fenced block was invisible**, and `MAX_RETRIES`
   was invisible everywhere: `_CODEY` matched snake_case and PascalCase but not
   SCREAMING_SNAKE. A section demonstrating repository behaviour in a fence
   therefore named zero identifiers and passed Gate A carrying no evidence. This
   was a genuine leak, not a false alarm.
6. **Diagram anchors were validated but did not count.** `_check_anchors` always
   read them while three sibling rules read only `section.anchors`, so a section
   whose sole citation lived on its diagram was failed for having no anchor.
7. **`notgithub.com` parsed as GitHub.** Neither remote regex had a left-hand
   host delimiter, so any host merely *ending* in `github.com` was accepted —
   and every lookup would then ask api.github.com about a repository living
   somewhere else and attach the answer as evidence.

Also closed from the same pass: Stripe, npm, PyPI and Azure connection-string
formats had no secret pattern at all; bare `token` and `secret` were missing
from the assigned-credential keyword list (the same value was caught under
`password` and missed under `token`); and the OpenAI `sk-` rule allowed hyphens
in the tail, so an ordinary service slug like `sk-metrics-collector-prod-01`
read as a credential — a detector that cries wolf is one that gets switched off.

Checked and found clean: float accumulation against ceilings (14 cost shapes, no
drift), timezone handling of `Charge.at` (naive, offset, bare date and garbage
all normalise correctly), and CRLF private-key blocks.

Deliberately *not* fixed: a symbol name split across a line break
(`Retry\nPolicy`). Joining adjacent capitalised words across newlines would
over-fire on ordinary prose, and the cost of the miss is lower than the cost of
a noisy rule.

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
- **Progress goes to stderr, as events, never as prints.** stdout carries the
  contract — artifact paths and `--json` payloads — so an adapter piping it into
  a parser must not receive chatter. The engine emits typed `Event`s carrying
  `seq`/`done`/`total` and never formats; the CLI, a JSONL sink and (later) MCP
  `notifications/progress` are interchangeable sinks over the same stream. This
  is why MCP needs no new instrumentation when it arrives.
- **`auto` is quiet when stderr is not a terminal.** An unattended run should
  not fill a log with lines nobody reads; `--progress plain` forces them on.
- **Ranking signals must be normalised against each other, not weighted.**
  Relevance is a fraction of the *question's* terms, so it tops out low — the
  best possible lexical match on a real repository measured **0.33** while
  normalised centrality reaches **1.00**, and `0.7 x 0.33` loses to `0.3 x 1.00`.
  A perfect match could not outrank a merely-central symbol. Both signals are
  now scaled to their own corpus maximum. Re-tuning the weights cannot fix
  incomparable scales.
- **The candidate pool is the whole repository.** Ranking relevance inside a
  pool centrality already chose cannot surface anything centrality missed. This
  is why 5cd573e's fix was incomplete: it fixed the ranking, not the pool, and
  the bug stayed invisible on kreb itself, where the top-80 pool was 16% of the
  repo instead of 3%.
- **Class members and test symbols are not planned as sections.** Nearly half
  the symbols in a repository are members, and a section per member is padding;
  members are promoted to their owner and deduplicated. Test names restate a
  feature's vocabulary more densely than the feature does, so once relevance
  means what it says, tests win — a section explaining a repository by its test
  names is a section about the wrong artifact. Both stay citable as evidence.
- **The planner is sensitive to question phrasing, by design and not by
  accident.** It is model-free lexical ranking; a question built from vocabulary
  that is generic *in that codebase* ("HTTP router") dilutes across every match,
  while a rare term ("reranking") is diagnostic. Fixing this properly means IDF,
  which is deferred: the dry-run is free, so a bad plan costs nothing to catch.
- **Gate B is conducted on a repository the reader knows cold, never on kreb.**
  On kreb's own code every claim is novel to its author, so the ≥3 bar passes by
  construction, and nothing is cheaply checkable, so the zero-wrong bar cannot be
  tested at all. A gate that cannot fail is not a gate.
- **The Gate B harness scores nothing.** Novelty is not observable from inside
  the artifact, and asking the pipeline to grade its own truthfulness on the one
  axis it is being tested for is a mirror, not a gate. What is automatable is the
  *cost* of judging: the sheet puts each claim beside the source its anchors cite,
  so the reader decides and never navigates. `kreb gate-b` exits 0 always.
- **No default spend cap.** Every ceiling is optional. The engine does not
  truncate research to hit a number nobody chose.
- **A ceiling cannot be exact.** A call's cost is unknown until it returns, so
  `max_per_run` means "start no new call once passed"; overshoot is bounded by
  one call, and that bound is what the test asserts.
- **Beats come before prose, and both renderers descend from them.** v0.1 ran
  `narration → storyboard` — write the script, then find pictures. That edge
  points the wrong way and guarantees a narrator describing what the screen is
  not showing. `beats` is the shared plan; audio and video share not one sentence.
- **A beat's flags are derived, never authored.** `confidence` and `kind` are
  copied off the source section; `hedge_required` and `prefix_required` are
  *properties*, not fields, so no model and no caller can set them. Same
  invariant as "the model never authors an `Anchor`", and for the same reason —
  the hedge validator is only sound if what it checks against could not have
  been written by the thing it is checking.
- **The hedge rule is stated positively because only positive rules are
  enforceable.** A `speculative` segment must *contain* a word from `HEDGES`.
  "Must not sound overconfident" is the same intent, unenforceable, and a model
  told to avoid sounding a way simply stops sounding that way.
- **The background signpost is prepended, not requested.** Where hedging must
  come from the model — you cannot mechanically insert "probably" and get
  English — a signpost can just be prefixed, which makes it structural rather
  than omittable. Same move as duration being a computed field.
- **The TTS cache key includes the engine identity**, which for piper means the
  binary version *and* a hash of the voice model file. Without it, upgrading
  piper and editing one paragraph yields one audible timbre seam mid-document
  that no artifact hash catches.
- **`tts/` is a port, and `SilenceEngine` is not only a test double.** It emits
  silence of the right duration, so concatenation, probing and the timings
  artifact are buildable and checkable on a machine with no TTS at all — which
  is most machines. Every timing it produces is flagged `estimated`, so a
  word-count estimate can never be mistaken for a measurement.
- **Two hosts, not one narrator, and that is the default.** A monologue that
  obeys the self-containment rule reads as a well-ordered *list* — eighteen true
  statements in a row is a reference, not something you follow while doing the
  dishes. A host who asks the question the listener is forming supplies the
  connective tissue the monologue is forbidden from having, without the expert's
  lines losing the independence the video renderer needs, because the question is
  a separate turn. `--style monologue` keeps the old shape.
- **Naturalness comes from the prompt; only fabrication is validated.** The
  first version made the host end every turn in a question mark, capped it at one
  sentence, and required at least one question per script. Those are style rules
  dressed as validators and they produce the stilted output they look like they
  prevent. They are gone. What survives is the *symbol allowlist*, which binds
  the host exactly as it binds the expert: set containment against the index,
  mechanically true or false. The general rule — validate what is checkable,
  prompt for what is not.
- **The prompt asks for a recording, not a document.** Hesitation, repetition,
  half-finished sentences, "right, right" while someone thinks — all of it reads
  badly and sounds human, and audio is the target. Instructions against filler
  were removed: filler keeps a conversation moving out loud. Two things stay
  off-limits because nothing downstream can catch them — facts that are not in
  the beats, and an invented world (weather, news, "I was reading this morning").
  A fabricated anecdote has no anchor and no symbol to check it against, so it is
  a fabrication however charming. Small talk that claims nothing is fine.
- **The sentence cap is a cut, not a rule.** A segment is the TTS cache unit and
  the video scene unit, so it must stay short — but a long stretch where the
  expert runs with it is one of the things that makes audio sound like a person.
  So a long expert turn is split into scene-sized segments rather than rejected:
  the words are the model's, the segmentation is ours. Host turns are never
  split — "Wait, hang on. Not even a little?" is three sentences and one breath,
  and cutting it puts a scene boundary inside a single thought.
- **Length is decided in `beats`, not in narration.** The first real run made
  the arithmetic concrete: 8 sections became 8 beats, 784 words, 4.8 minutes, at
  a measured 164 wpm. Forty minutes is ~6,500 words is ~67 beats, so a length
  target that only reached the narration prompt would produce eight padded beats
  — the same script read slower. `render/shape.py` carries three presets
  (`quick` 5min / `standard` 15min / `deep` 40min) and the beat count reaches the
  planner. `quick` computes to exactly the 8 beats the real run produced, which
  is the only reason the other two can be trusted to scale.
- **Depth and length are one knob.** They are not independent: a document holds
  only so much surface-level material, so a long shallow script repeats itself
  and a deep five-minute one is a list of mechanisms with no room to explain any.
  Each preset names an audience and what the expert reaches for, and the word
  count is a *target* in the prompt — never a check, because rejecting a script
  for length would be enforcing a budget on writing.
- **The critique can be handed back, but only its quoted lines.** `--revise`
  runs one rewrite from the findings, and the scores and summary are withheld: a
  model told it scored 2 on "natural" has a target with no text attached, and the
  cheapest way to move it is to write something blander no judge objects to. A
  quoted finding points at a line verifiably in the script, which is closer to
  "this symbol does not resolve" than to "this reads badly". It stays opt-in and
  both drafts are kept (`narration.draft.json`, `critique.draft.json`), because
  models revise toward safe and a revision that made things worse has to be
  visible rather than silently blessed.
- **The computed opening defers to the model's.** The prompt now asks for a real
  podcast opening, so beat zero usually arrives as one; prepending a bare
  question in front of that gives two openings. `_covers` checks content-word
  overlap — approximate on purpose, since the cost of being wrong is one
  redundant sentence, not a wrong claim. A script that dives straight into
  mechanism still gets the question.
- **The judge has to agree with the writer about what good audio is.** Scoring
  "substance" penalized filler, which would have pulled scripts back toward the
  stilted thing the prompt stopped asking for. The axes are now natural /
  coherent / listenable, and the judge is told explicitly not to mark down
  hesitation and repetition.
- **Coherence is judged by a model and reported, never fed back.**
  `render/critique.py` scores a script 1–5 on natural / coherent / setup /
  substance and writes `critique.json`. It is deliberately *not* in the retry
  loop: `research/writer.py` sets the rule that no rejection reason is ever a
  semantic judgement, because a model told "this sounds stilted" does not become
  natural — it becomes something that does not read as stilted to the judge. Two
  guards on the judge itself: it never sees the document, so it cannot reward a
  script for agreeing with its source (that is Gate A's job, done with anchors),
  and every finding must quote the script verbatim or it is dropped, because a
  well-argued critique of a line nobody wrote is what a bad judge produces.
- **`Cast` is itself a `SpeechEngine`, so one voice is a cast of one.** There is
  no second path through synthesis: same loop, same cache, same measurement,
  whether one person is talking or two. Its identity names every voice, so
  recasting invalidates the cache rather than serving back the old host.
- **A dialogue that cannot be split says so.** Silence has no timbre and piper
  needs a second model file, so neither can be re-voiced by name. Collapsing
  quietly would leave someone reading a two-speaker transcript and hearing one.
- **The default voice is hosted and free.** `deepgram/flux-tts:free` on the
  OpenRouter key the project already uses — priced "0" on both prompt and
  completion, so the cost of `kreb audio` is exactly zero and needs no lookup.
  A default that cost money per paragraph would make narration something you
  think twice about running. Piper stays as the offline adapter rather than
  being deleted: a hosted alias can be re-pointed at a new build without
  anything here noticing, and a hashed local voice cannot.
- **The hosted engine asks for `mp3`, not the endpoint's default `pcm`.** The
  PCM it returns is headerless — no rate, no channel count, nothing `ffprobe`
  can read — and a pipeline whose central promise is *durations are measured*
  cannot accept a stream that cannot be measured. Re-encoding to wav at a
  declared rate is also what makes the model's own output rate stop mattering.
- **`--voice` is dispatched by shape, not by a separate backend selector.**
  `silence`, `*.onnx`, or a slashed model id; anything else is refused by name.
  `.onnx` is tested before the slash rule, because a piper path usually has a
  slash too and being read as a model id posts a filesystem path to an API.
- **A hosted segment carries its `generation_id`, not a price** — and it reaches
  `timings.json`, because an id that stops at the engine boundary is a promise of
  a reconcilable trail with no trail. Resolving cost is one extra request per
  segment against an endpoint that lags the generation, and on the free default
  the cost is exactly zero. An unreconciled receipt is honest about being
  unreconciled; a guessed number would not be. Cached segments carry no id: they
  made no request, and copying the first run's id onto them would double-count
  the one real charge.
- **A missing voice must not cost you the writing.** `kreb audio` still writes
  beats, script and an estimated timeline, names what is missing, and exits 1.
  `--json` returns the same exit code as the human path.
- **Artifact reuse is keyed on identity, never on a file existing.** A beats
  plan carries a digest of the document it was built from, and `--out` defaults
  to one fixed path, so existence-based reuse means narrating your second
  document replays the first one's beats. A plan with no digest counts as a
  mismatch: regenerating costs cents, narrating the wrong document costs the
  artifact.
- **`ok` and `complete` are different questions.** A file that plays cleanly and
  is missing one section is not a success. `beats` requires every section to get
  a beat; a segment dropped at synthesis undoes that a layer down, so callers
  exit on `complete` and the dropped segments are named in `--json`.
- **Beats and narration are plain files under `.kreb/audio/`, not store nodes.**
  Every other generated node goes through `GeneratedKey` /
  `materialize_generated`. The digest guard closes the practical hole; moving
  them into the store is a later cleanup, recorded so it is not mistaken for an
  oversight.
- **Duration is measured with `ffprobe`, never estimated from word count.**
  Downstream, `scene_len = max(audio_len, min_duration)`; a duration wrong by a
  second is a caption that outlives its scene.

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
4. **Does `beats` carry enough for two renderers to diverge, or does it collapse
   into a de-facto script?** Half-settled. The structure holds — a beat is one
   point, a narration line is one *rendering* of that point, and the audio
   renderer already adds material video would not need (a spoken title card, a
   spoken caveat, a signposted background prefix). The other half needs real
   model output on a real document and is not answerable from here. Fold one
   `kreb audio` run into the Gate B follow-up rather than running it separately.
5. **The hedge and signpost rules have never fired on real output.** The kreb
   document is 6 `verified` / 2 `derived`, 6 `structure` / 2 `rationale` — zero
   `speculative`, zero `background`. Both rules are mutation-verified in tests
   and unexercised by the corpus. A document that hedges nothing is either a
   confident document or a broken confidence signal, and that is worth knowing.

6. **The judge paid for itself on the first real run.** Three findings, and the
   two worst were in computed segments rather than model output: a spoken
   closing that read `No pull-request evidence: forge unreachable. 14 files had
   no symbol index` — a log line nobody says aloud — and an opening where the
   expert answered the document's question with a disclaimer, so the model asked
   the same question again two lines later. Both fixed: `spoken_warnings()` is a
   parallel phrasing of `warnings()` for the ear, and the opening is now the
   question alone, with the expert's first words being the model's. The third
   finding was the model's — five host turns built as "And the X —" — and it is
   the prompt's job, since nothing mechanical catches an identical frame around
   casual words. A partial verdict also passed as good enough: the judge scored
   two of three axes and the mean landed on the threshold, so `good_enough` now
   requires a complete one.

7. **Is the judge calibrated?** `GOOD_ENOUGH = 3.0`
   was chosen as "clearly mediocre", not tuned — nothing has enough runs behind
   it to tune against. Two things need a real run to answer: whether the scores
   track what a person would say about the same script, and whether a model
   scoring its own sibling's output is generous in a way that makes the number
   decorative. The honest fallback is human eval, and the critique's job is to
   make that cheap by pointing at specific lines rather than to replace it.

8. **The hosted voice has been used in anger once.** Built against the
   documented contract and the model listing, exercised end-to-end against real
   mp3 bytes through the real decode path, and never once called. Three things
   are unknown and only one keyed run answers all of them: whether
   `deepgram/flux-tts` requires a `voice` (it is sent only when set, so the
   first failure will say so), what the `:free` rate limit is per day and how a
   429 body reads, and how long a request may be — an 18-segment narration is
   18 requests, and if there is a per-minute cap the run will hit it. None of
   these block the code: a capped run fails per segment with the body text as
   its reason, and the per-segment cache means rerunning resumes where it
   stopped. Fold it into the same keyed session as Gate B.
