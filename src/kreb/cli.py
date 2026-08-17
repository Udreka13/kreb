"""The command line — the real integration boundary.

Stable JSON in, artifact paths out. Every adapter (an MCP server, an editor
plugin, CI) shells out to this rather than reimplementing the pipeline, which
is what keeps prompt text and format knowledge inside the engine where they can
stay consistent with the schema.

`argparse`, not `typer`/`rich`. The dependency list is three packages, this
surface is small enough not to need a framework, and a tool people install
globally is a tool whose dependency tree they inherit.

Commands that do not spend money — `index`, `map`, `validate`, `render` — work
with no API key at all. That is deliberate: it means the deterministic half of
kreb is usable, testable and debuggable before anyone has an account.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kreb.doc.gate_a import run as gate_a
from kreb.doc.schema import Document
from kreb.doc.validate import validate
from kreb.index.repo_index import build_index
from kreb.index.repo_map import build_map
from kreb.repo.access import Repository


def _repo(args) -> Repository:
    return Repository(Path(args.repo).resolve(), rev=args.rev)


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")


def cmd_index(args) -> int:
    repo = _repo(args)
    index = build_index(repo, include_vendored=args.include_vendored)
    degraded = sum(1 for f in index.files.values() if f.degraded)
    payload = {
        "sha": index.sha,
        "files": len(index.files),
        "symbols": len(index.symbols),
        "degraded_files": degraded,
        "languages": sorted({f.language for f in index.files.values() if f.language}),
        "warnings": repo.caps.warnings(),
    }
    if args.json:
        _emit(payload, as_json=True)
    else:
        print(f"commit    {index.sha[:12]}")
        print(f"files     {len(index.files)} ({degraded} without a symbol index)")
        print(f"symbols   {len(index.symbols)}")
        print(f"languages {', '.join(payload['languages']) or 'none'}")
        for warning in payload["warnings"]:
            print(f"warning:  {warning}")
    return 0


def cmd_map(args) -> int:
    repo = _repo(args)
    index = build_index(repo)
    repo_map = build_map(index)
    if args.json:
        _emit(
            {
                "sha": index.sha,
                "total_files": repo_map.total_files,
                "total_symbols": repo_map.total_symbols,
                "entry_points": repo_map.entry_points,
                "central_symbols": [
                    {"ref": ref, "score": round(score, 5)}
                    for ref, score in repo_map.central_symbols[: args.limit]
                ],
            },
            as_json=True,
        )
    else:
        print(repo_map.render(max_symbols=args.limit))
    return 0


def cmd_validate(args) -> int:
    """Check a document against the repository it claims to describe."""
    doc = Document.read(args.document)
    index = build_index(_repo(args))
    result = gate_a(doc, index)
    report = validate(doc, index)

    if args.json:
        _emit(
            {
                "passed": result.passed,
                "checks": [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "measured": c.measured,
                        "threshold": c.threshold,
                    }
                    for c in result.checks
                ],
                "manual_audits": [{"name": n, "threshold": t} for n, t in result.manual],
                "findings": [
                    {
                        "rule": f.rule,
                        "severity": f.severity,
                        "section": f.section_id,
                        "ref": f.ref,
                        "message": f.message,
                    }
                    for f in report.findings
                ],
            },
            as_json=True,
        )
    else:
        print(result.summary())
        if report.warnings:
            print("\nWarnings:")
            for finding in report.warnings:
                print(f"  {finding}")
        if report.failures:
            print("\nFailures:")
            for finding in report.failures:
                print(f"  {finding}")
    return 0 if result.passed else 1


def cmd_render(args) -> int:
    """Render an existing document. No model calls, no key required."""
    doc = Document.read(args.document)
    report = None
    if args.repo:
        try:
            report = validate(doc, build_index(_repo(args)))
        except Exception as exc:
            print(f"note: could not check freshness against the repo ({exc})", file=sys.stderr)

    if args.format == "md":
        from kreb.render.markdown import render
    else:
        from kreb.render.html import render

    output = render(doc, report)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output, encoding="utf-8")
        if args.json:
            _emit({"path": str(destination.resolve()), "format": args.format}, as_json=True)
        else:
            print(destination.resolve())
    else:
        sys.stdout.write(output)
    return 0


def cmd_doc(args) -> int:
    """Run research and write a document. This is the command that spends money."""
    from kreb.budget.ledger import Ledger
    from kreb.budget.policy import Budget
    from kreb.config.secrets import MissingCredential, resolve_api_key
    from kreb.doc.schema import Capabilities
    from kreb.provider.metered import MeteredProvider
    from kreb.provider.openrouter import OpenRouterProvider
    from kreb.progress import reporter_for
    from kreb.research.loop import PlannedSection, run_research
    from kreb.store.store import ArtifactStore

    repo = _repo(args)
    index = build_index(repo)
    repo_map = build_map(index)

    try:
        api_key = resolve_api_key()
    except MissingCredential as exc:
        print(str(exc), file=sys.stderr)
        return 2

    kreb_dir = Path(args.repo) / ".kreb"
    store = ArtifactStore(kreb_dir)
    ledger = Ledger(kreb_dir / "spend.jsonl")
    budget = Budget(max_per_run=args.max_cost, warn_at=0.8)
    provider = MeteredProvider(
        inner=OpenRouterProvider(api_key=api_key, profile=args.profile),
        ledger=ledger,
        budget=budget,
    )

    plan = plan_sections(repo_map, index, question=args.question, depth=args.depth)
    if not plan:
        print("nothing to write: the repository has no indexed symbols", file=sys.stderr)
        return 1

    degraded = sum(1 for f in index.files.values() if f.degraded)
    caps = Capabilities(
        base_sha=index.sha,
        git="shallow" if repo.caps.shallow else "ok",
        forge="none",
        web="off",
        languages=tuple(sorted({f.language for f in index.files.values() if f.language})),
        degraded_files=degraded,
        total_files=len(index.files),
        dirty=repo.caps.dirty,
    )

    # Progress goes to stderr so stdout keeps carrying only the contract:
    # artifact paths and --json payloads.
    reporter = reporter_for("none" if args.quiet else args.progress, sys.stderr)

    report = run_research(
        plan=plan,
        question=args.question,
        index=index,
        repo=repo,
        provider=provider,
        store=store,
        capabilities=caps,
        title=args.title or f"{Path(args.repo).resolve().name}: {args.question}",
        reporter=reporter,
    )

    out = Path(args.out or (kreb_dir / "docs" / "document.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    report.document.write(out)

    if args.json:
        _emit(
            {
                "path": str(out.resolve()),
                "sections": len(report.document.sections),
                "written": report.written,
                "reused": report.reused,
                "failed": {k: v[-1:] for k, v in report.failed.items()},
                "skipped": report.skipped,
                "stopped_early": report.stopped_early,
                "stop_reason": report.stop_reason,
                "cost": round(report.cost, 6),
            },
            as_json=True,
        )
    else:
        print(report.summary())
        print(out.resolve())
    return 0 if report.complete else 1


# Words that carry no signal about which symbols a question is about.
_STOPWORDS = frozenset(
    """the a an and or but of for to in on at by with from is are was were be been
    how why what which when where who does do did can could should would will shall
    this that these those it its as if then than so such not no nor kreb code
    codebase repo repository work works working use used using handle handled""".split()
)


def _terms(text: str) -> set[str]:
    """Content words from a question, split so `snake_case` matches too."""
    import re

    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _relevance(question_terms: set[str], ref: str) -> float:
    """How much a symbol's name and path overlap the question's content words."""
    if not question_terms:
        return 0.0
    haystack = _terms(ref.replace("/", " ").replace("#", " ").replace(".", " "))
    if not haystack:
        return 0.0
    hits = sum(
        1
        for term in question_terms
        if term in haystack or any(term in word or word in term for word in haystack)
    )
    return hits / len(question_terms)


def plan_sections(repo_map, index, *, question: str, depth: int) -> list:
    """A deterministic outline, ranked by relevance to the question.

    Model-free on purpose: `kreb doc` then has exactly one place where
    nondeterminism enters — the section writer — and the same question against
    the same commit plans the same sections every time.

    Centrality alone is not enough. Ranking purely by it answers "what is this
    repository built around", which is a fine question and usually not the one
    that was asked; a document about the eight most-imported symbols is a
    plausible-looking non-answer. Relevance leads and centrality breaks ties, so
    a question with no lexical hits still degrades to the central symbols rather
    than to nothing.
    """
    from kreb.research.loop import PlannedSection

    question_terms = _terms(question)
    pool = repo_map.central_symbols[:80] or []
    if not pool:
        return []

    top_score = max((score for _ref, score in pool), default=1.0) or 1.0
    ranked = sorted(
        pool,
        key=lambda item: (
            -(0.7 * _relevance(question_terms, item[0]) + 0.3 * (item[1] / top_score)),
            item[0],
        ),
    )

    wanted = max(3, depth * 4)
    plan = []
    for position, (ref, _score) in enumerate(ranked[:wanted]):
        symbol = index.resolve(ref)
        if symbol is None:
            continue
        plan.append(
            PlannedSection(
                id=f"s{position:02d}-{symbol.name.lower().replace('_', '-')[:40]}",
                # Archaeology is the expensive-but-free part, so it is spent on
                # the most relevant symbols rather than an arbitrary prefix.
                kind="rationale" if position < depth else "structure",
                title=symbol.qualname,
                refs=[ref],
            )
        )
    return plan


def _plan_from_map(repo_map, index, *, depth: int, question: str = "") -> list:
    return plan_sections(repo_map, index, question=question, depth=depth)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kreb", description=__doc__.split("\n")[0])
    parser.add_argument("--repo", default=".", help="repository to work on")
    parser.add_argument("--rev", default="HEAD", help="commit to pin the run to")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="index the repository and report what was found")
    p_index.add_argument("--include-vendored", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_map = sub.add_parser("map", help="print the deterministic repository map")
    p_map.add_argument("--limit", type=int, default=25)
    p_map.set_defaults(func=cmd_map)

    p_doc = sub.add_parser("doc", help="research the repository and write a document")
    p_doc.add_argument("question", help="what the document should answer")
    p_doc.add_argument("--depth", type=int, default=2)
    p_doc.add_argument("--profile", default="balanced", choices=["budget", "balanced", "max"])
    p_doc.add_argument("--max-cost", type=float, default=None,
                       help="ceiling in currency; omitted means no ceiling")
    p_doc.add_argument("--title", default="")
    p_doc.add_argument("--out", default="")
    p_doc.add_argument(
        "--progress",
        default="auto",
        choices=["auto", "plain", "json", "none"],
        help="progress on stderr: auto (human on a terminal), plain (always), "
        "json (JSON Lines, for adapters), none",
    )
    p_doc.add_argument("--quiet", action="store_true", help="no progress output")
    p_doc.set_defaults(func=cmd_doc)

    p_render = sub.add_parser("render", help="render an existing document")
    p_render.add_argument("document")
    p_render.add_argument("--format", default="md", choices=["md", "html"])
    p_render.add_argument("--out", default="")
    p_render.set_defaults(func=cmd_render)

    p_validate = sub.add_parser("validate", help="run Gate A against the repository")
    p_validate.add_argument("document")
    p_validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted; completed work has been saved", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
