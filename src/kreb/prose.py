"""Splitting prose into sentences, in the one way this project needs it.

This lives at the top level rather than under `doc/` or `render/` because two
unrelated consumers need exactly the same behaviour: the Gate B worksheet splits
a section into judgeable claims, and the narration renderer checks that a
segment is short enough to be a scene. Both are wrong in the same way if a
period inside `os.path.join` or `v2.13` ends a sentence, so the rule is defined
once and both import it.
"""

from __future__ import annotations

import re

# Splits after `.`, `?` or `!` followed by whitespace and a capital or a
# backtick, which keeps `path/to/file.py` and `v2.13` intact.
_SENTENCE = re.compile(r"(?<=[.?!])\s+(?=[A-Z`])")
_CODE_SPAN = re.compile(r"`[^`]*`")


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends that fall outside inline code spans."""
    # Code spans are masked to a same-length run of `x` so string offsets stay
    # valid against the original text — the split points are found in the mask
    # and the slices are taken from the real thing.
    masked = _CODE_SPAN.sub(lambda m: "x" * len(m.group()), text)
    out, start = [], 0
    for match in _SENTENCE.finditer(masked):
        out.append(text[start : match.start()].strip())
        start = match.end()
    out.append(text[start:].strip())
    return [s for s in out if s]
