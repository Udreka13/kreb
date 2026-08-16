# kreb

A codebase comprehension pipeline. One research pass produces a structured,
symbol-anchored document explaining a repository — especially *why* the code is
shaped the way it is, mined from git blame, PR threads and reverted commits.
Renderers turn that document into artifacts suited to different attention
budgets: a self-contained HTML page, an audio overview, a video overview.

Named after Creb, the Mog-ur in *Clan of the Cave Bear* — one-eyed, crippled,
useless on a hunt, and the most important member of the clan because he holds
its memories and interprets them for people who cannot reach them directly.

**Status: early. Not yet usable.**

## The idea in one paragraph

Tools that auto-generate documentation from a repository are widely criticised
for the same four things: confident hallucination presented as authority, going
stale silently, claims about the repo with nothing behind them, and diagrams not
tied to the implementation. kreb's design answers each of those directly — every
claim carries an evidence chain and a confidence tier, `verified` means read off
the AST, sections declare the symbols they depend on so staleness is detectable,
and extracted diagrams come from AST traversal rather than a model's belief.

## Design documents

| Document | What it settles |
|---|---|
| [`idea/docs/kreb-prd.md`](idea/docs/kreb-prd.md) | Product requirements |
| [`idea/docs/pmf-research.md`](idea/docs/pmf-research.md) | Market, competition, demand, cost |
| [`idea/docs/tooling-survey.md`](idea/docs/tooling-survey.md) | Libraries: adopt / evaluate / avoid |
| [`idea/docs/architecture.md`](idea/docs/architecture.md) | **Current architecture** |

## Development

```sh
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## License

MIT
