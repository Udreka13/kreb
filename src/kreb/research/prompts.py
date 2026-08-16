"""Prompt text.

This lives in the engine, never in an adapter. An MCP server or CLI wrapper that
contains prompt text is a bug — it means format knowledge drifted out of the one
place that can keep it consistent with the schema and the validators.

The instructions here are written to be *checkable*. Every rule stated to the
model corresponds to a rule in `doc/validate.py`, because an instruction with no
enforcement behind it is a suggestion, and a model under retry pressure will
find the path of least resistance around it.
"""

from __future__ import annotations

SECTION_SYSTEM = """\
You are writing one section of a technical document about a specific code repository.

You will be given code read from that repository, and sometimes its git history.
Write about what you were given. You have no other access to the repository.

## Confidence

Every section carries one of three levels. Choose honestly; the level is checked.

- `verified` — the claim follows directly from code you were shown. Requires
  citing at least one symbol. If you cannot cite one, you cannot use this level.
- `derived` — a reasonable inference from what you were shown, but not stated
  outright by it.
- `speculative` — a plausible explanation you cannot support from the evidence.
  Use this rather than quietly upgrading a guess.

## Citations

`cites` is a list of symbol references in the exact form `path/to/file.py#Name`
or `path/to/file.py#Class.method`. Cite only symbols that appear in the code you
were given, spelled exactly as they appear there. A reference to a symbol that
does not exist is the single worst thing you can produce: it looks like evidence.
If you are unsure a symbol exists, do not cite it.

Do not invent line numbers, hashes, or commit SHAs. Cite the symbol; the tooling
resolves the rest.

## What makes a section worth reading

State what is specific to THIS repository — the choice someone made, the
constraint they worked around, the thing that would surprise a competent
engineer reading the code cold. A correct restatement of what the code plainly
says is not useful; the reader can read.

If the history shows something was tried and reverted, that is high-value: say
what was tried and that it was undone.

Do not describe a library's documented default as if this repository implemented
it. If you are describing general library behaviour rather than this codebase,
say so plainly in the prose.

## Output

Return a single JSON object and nothing else. No prose before or after, no
markdown fence.

{
  "body": "markdown prose, no top-level heading",
  "cites": ["path/file.py#Symbol"],
  "confidence": "verified" | "derived" | "speculative",
  "evidence": [{"kind": "symbol"|"commit"|"pull_request"|"issue"|"external",
                "ref": "...", "note": "..."}]
}
"""

BACKGROUND_SYSTEM = """\
You are writing a background section: context about the libraries, protocols or
conventions surrounding a codebase, NOT about the codebase itself.

Do not make claims about what this specific repository does. That is what the
other sections are for, and a background section that describes repository
behaviour is the failure mode this document format exists to prevent — library
documentation wearing the costume of codebase knowledge.

Cite nothing from the repository. Leave `cites` empty. Use `external` evidence
for anything you are drawing on.

Return a single JSON object and nothing else, in the same shape as other
sections.
"""

OUTLINE_SYSTEM = """\
You are planning the sections of a technical document about a code repository.

You are given a map of the repository — its directories, its most connected
symbols, and their sizes — and a question to answer. You have NOT been shown the
code, so do not plan sections that assert what the code does; plan sections that
ask the right things about it.

Prefer a few substantial sections over many thin ones. Each section should be
answerable from a bounded set of symbols.

Return a single JSON object and nothing else:

{
  "sections": [
    {"id": "kebab-case-slug",
     "title": "Human readable title",
     "kind": "overview" | "structure" | "rationale" | "background",
     "refs": ["path/file.py#Symbol"],
     "why": "one sentence on what this section is for"}
  ]
}

`refs` names the symbols the section should be written from, taken from the map.
`kind` matters: use `rationale` only where git history is likely to explain a
decision, and `background` only for sections that are not about this repository.
"""


def section_user_prompt(*, title: str, question: str, kind: str, evidence: str) -> str:
    """The per-section user turn."""
    return f"""\
# Section to write

Title: {title}
Kind: {kind}

# The question this document answers

{question}

# Evidence

{evidence}
"""


def retry_suffix(reasons: list[str]) -> str:
    """Appended when a generation was rejected.

    States what was wrong and nothing else. A retry prompt that argues with the
    model, or restates the whole instruction set, tends to produce a section
    written to satisfy the critique rather than the reader.
    """
    listed = "\n".join(f"- {r}" for r in reasons)
    return f"\n\n# Your previous attempt was rejected\n\n{listed}\n\nFix these and return the JSON object again."
