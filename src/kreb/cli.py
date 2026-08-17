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
import re
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


def cmd_gate_b(args) -> int:
    """Build the Gate B worksheet. No model calls, no key required.

    Deliberately exits 0 whatever the document says: this command produces the
    sheet, and the verdict is the reader's. An exit code here would be the
    pipeline grading itself on the one axis it is being tested for.
    """
    from kreb.doc.gate_b import build as build_sheet
    from kreb.render import worksheet as worksheet_render

    doc = Document.read(args.document)
    repo = _repo(args)
    sheet = build_sheet(doc, build_index(repo), repo)

    if args.json:
        _emit(
            {
                "claims": len(sheet.claims),
                "verified_claims": len(sheet.verified_claims),
                "thresholds": {"novel_true": 3, "wrong_at_verified": 0},
                "caveats": list(sheet.caveats),
            },
            as_json=True,
        )
        return 0

    render = worksheet_render.render_markdown if args.format == "md" else worksheet_render.render
    output = render(sheet)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output, encoding="utf-8")
        print(destination.resolve())
    else:
        sys.stdout.write(output)
    return 0


def cmd_audio(args) -> int:
    """Turn a document into spoken narration, and into audio if a voice exists.

    Three artifacts, deliberately separable: `beats.json` (what gets said, in
    what order), `narration.json` (the words), and the wav plus `timings.json`.
    The first two cost model calls and are the part with judgement in them; the
    last two cost nothing and are pure mechanism.

    When no speech engine is available this still writes the beats, the
    narration and an estimated timeline, and says what is missing — the same
    partial-that-says-so contract the research loop uses when it hits a ceiling.
    A missing voice must not cost you the writing you already paid for.
    """
    from kreb.budget.ledger import Ledger
    from kreb.budget.policy import Budget
    from kreb.config.secrets import MissingCredential, resolve_api_key
    from kreb.progress import Progress, reporter_for
    from kreb.provider.metered import MeteredProvider
    from kreb.provider.openrouter import OpenRouterProvider
    from kreb.render import beats as beats_mod
    from kreb.render import narration as narration_mod
    from kreb.render.audio import build_audio, timings_json
    from kreb.tts.piper import PiperEngine
    from kreb.tts.silence import SilenceEngine

    doc = Document.read(args.document)
    repo = _repo(args)
    index = build_index(repo)

    kreb_dir = Path(args.repo) / ".kreb"
    out_dir = Path(args.out) if args.out else kreb_dir / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = (
        SilenceEngine()
        if args.voice == "silence"
        else PiperEngine(voice=Path(args.voice) if args.voice else None)
    )

    reporter = reporter_for("none" if args.quiet else args.progress, sys.stderr)
    progress = Progress(reporter)

    beats_path = out_dir / "beats.json"
    narration_path = out_dir / "narration.json"

    if beats_path.exists() and narration_path.exists() and not args.regen:
        # Re-synthesizing is free; re-writing is not. Reusing the prose by
        # default is what makes "try a different voice" a cheap experiment.
        plan = beats_mod.from_json(beats_path.read_text(encoding="utf-8"))
        narration = narration_mod.from_json(narration_path.read_text(encoding="utf-8"))
        spent = 0.0
    else:
        try:
            api_key = resolve_api_key()
        except MissingCredential as exc:
            print(str(exc), file=sys.stderr)
            return 2

        ledger = Ledger(kreb_dir / "spend.jsonl")
        provider = MeteredProvider(
            inner=OpenRouterProvider(api_key=api_key, profile=args.profile),
            ledger=ledger,
            budget=Budget(max_per_run=args.max_cost, warn_at=0.8),
            phase="render",
        )

        progress.emit("beats_started", "planning what to say", total=len(doc.sections))
        planned = beats_mod.plan_beats(doc, index, provider, progress=progress)
        if not planned.ok:
            print("could not plan beats:", file=sys.stderr)
            for reason in planned.rejections[-4:]:
                print(f"  - {reason}", file=sys.stderr)
            return 1
        plan = planned.plan
        beats_path.write_text(beats_mod.to_json(plan), encoding="utf-8")

        progress.emit("narration_started", "writing the script", total=len(plan.beats))
        written = narration_mod.write_narration(
            plan, index, provider, document=doc, progress=progress
        )
        if not written.ok:
            print("could not write narration:", file=sys.stderr)
            for reason in written.rejections[-4:]:
                print(f"  - {reason}", file=sys.stderr)
            return 1
        narration = written.narration
        narration_path.write_text(narration_mod.to_json(narration), encoding="utf-8")
        spent = planned.cost + written.cost

    (out_dir / "script.txt").write_text(narration.script + "\n", encoding="utf-8")

    result = build_audio(
        narration,
        engine,
        out=out_dir / "narration.wav",
        cache_dir=kreb_dir / "tts",
        progress=progress,
    )
    (out_dir / "timings.json").write_text(timings_json(result), encoding="utf-8")

    if args.json:
        _emit(
            {
                "beats": str(beats_path.resolve()),
                "narration": str(narration_path.resolve()),
                "timings": str((out_dir / "timings.json").resolve()),
                "audio": str(result.path.resolve()) if result.path else None,
                "engine": result.engine,
                "segments": len(narration.segments),
                "seconds": round(result.seconds, 2),
                "estimated": result.estimated,
                "synthesized": result.synthesized,
                "reused": result.reused,
                "reason": result.reason,
                "cost": round(spent, 6),
            },
            as_json=True,
        )
        # Same exit code as the human path. Returning 0 here because the JSON
        # was emitted successfully would make `--json` the flag that hides
        # failures from exactly the callers most likely to be scripting them.
        return 0 if result.ok else 1

    minutes, seconds = divmod(int(result.seconds), 60)
    length = f"{minutes}:{seconds:02d}" + (" (estimated)" if result.estimated else "")
    print(f"{len(narration.segments)} segments, {length}, ${spent:.4f}")
    if result.reason:
        print(f"no audio: {result.reason}", file=sys.stderr)
    for failure in result.failures[:5]:
        print(f"  {failure}", file=sys.stderr)
    print((result.path or narration_path).resolve())
    # Exit 1 when there is no audio: unlike Gate B, this command has a
    # mechanical definition of success and a caller scripting it needs to know.
    return 0 if result.ok else 1


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

    **The candidate pool is the whole repository, not a central prefix.** Ranking
    relevance inside a pool that centrality already chose cannot surface anything
    centrality missed, which makes the relevance weighting decorative on exactly
    the repositories where it matters. In a layered codebase the most-imported
    symbols are the domain entities, so "how does reranking work" scored the
    top-80 pool and returned domain models — a plausible-looking non-answer
    reached by a different route. Scoring every symbol is a few string
    comparisons per symbol and removes the failure entirely.
    """
    from kreb.research.loop import PlannedSection

    question_terms = _terms(question)
    pool = _plannable(repo_map.central_symbols or [])
    if not pool:
        return []

    # Both signals are normalised against the best value available in this
    # repository, because they are otherwise on incomparable scales and the
    # weights become decorative. Relevance is a fraction of the *question's*
    # terms, so a long question mechanically depresses every symbol's score: on
    # a real repository the best possible lexical match measured 0.33 against a
    # centrality of 1.00, and 0.7 x 0.33 loses to 0.3 x 1.00. A perfect match
    # could not outrank a merely-central symbol. Normalising both to their own
    # maxima asks the same question of each — how good is this, relative to the
    # best this repository offers — and makes the weights mean what they say.
    top_central = max((score for _ref, score in pool), default=1.0) or 1.0
    relevance = {ref: _relevance(question_terms, ref) for ref, _score in pool}
    # `or 1.0` is what makes a question with no lexical hits degrade to pure
    # centrality rather than divide by zero.
    top_relevance = max(relevance.values(), default=0.0) or 1.0

    ranked = sorted(
        pool,
        key=lambda item: (
            -(
                0.7 * (relevance[item[0]] / top_relevance)
                + 0.3 * (item[1] / top_central)
            ),
            item[0],
        ),
    )

    wanted = max(3, depth * 4)
    plan: list = []
    seen: set[str] = set()
    for ref, _score in ranked:
        if len(plan) >= wanted:
            break
        ref = _section_subject(ref, index)
        if ref in seen:
            continue
        symbol = index.resolve(ref)
        if symbol is None:
            continue
        seen.add(ref)
        position = len(plan)
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


_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__)/"
    r"|(?:^|/)conftest\.py$"
    r"|(?:^|/)test_[^/]*$"
    r"|_test\.[a-z]+$"
    r"|\.(?:test|spec)\.[jt]sx?$"
)


def _plannable(scored: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Drop test symbols from section candidacy — but not from the index.

    Test names restate a feature's vocabulary more densely than the feature
    does: `test_recorded_session_feeds_campaign` matches a question about
    recorded sessions feeding campaigns better than the transcriber that
    actually does it. Once relevance is scaled to mean what it says, tests win,
    and a section explaining a repository by its test names is a section about
    the wrong artifact.

    They stay in the index and remain citable as evidence — this decides only
    what deserves a section of its own.
    """
    kept = [item for item in scored if not _TEST_PATH.search(item[0].partition("#")[0])]
    # A repository that is all tests still gets a plan rather than nothing.
    return kept or scored


def _section_subject(ref: str, index) -> str:
    """Promote an attribute or method to the thing that contains it.

    Nearly half the symbols in a typical repository are class members, and a
    section per member is padding by construction — five sections on
    `RetrievedPassage.score`, `.page`, `.label` say together what one section on
    `RetrievedPassage` says once. Promoting rather than dropping keeps the
    signal when a *method* name matches the question and its class name does
    not; the caller deduplicates, so several members collapse onto one subject.
    """
    path, _, qualname = ref.partition("#")
    if "." not in qualname:
        return ref
    parent = f"{path}#{qualname.rpartition('.')[0]}"
    return parent if parent in index.symbols else ref


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

    p_gate_b = sub.add_parser(
        "gate-b", help="build the Gate B worksheet — the claims, and the code they cite"
    )
    p_gate_b.add_argument("document")
    p_gate_b.add_argument("--format", default="html", choices=["html", "md"])
    p_gate_b.add_argument("--out", default="")
    p_gate_b.set_defaults(func=cmd_gate_b)

    p_audio = sub.add_parser("audio", help="narrate a document, and speak it if a voice exists")
    p_audio.add_argument("document")
    p_audio.add_argument(
        "--voice",
        default="silence",
        help="path to a piper voice model, or `silence` for a timed placeholder track",
    )
    p_audio.add_argument("--profile", default="balanced", choices=["budget", "balanced", "max"])
    p_audio.add_argument("--max-cost", type=float, default=None)
    p_audio.add_argument("--out", default="", help="directory for the audio artifacts")
    p_audio.add_argument(
        "--regen", action="store_true", help="rewrite the beats and script instead of reusing"
    )
    p_audio.add_argument(
        "--progress", default="auto", choices=["auto", "plain", "json", "none"]
    )
    p_audio.add_argument("--quiet", action="store_true", help="no progress output")
    p_audio.set_defaults(func=cmd_audio)

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
