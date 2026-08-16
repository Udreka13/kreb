"""Secret detection for content that is about to enter a document.

The threat is specific and not hypothetical: the research agent reads the
repository and quotes it into an artifact the user commits or publishes. A
fixture with a live token, a `.env` that slipped past `.gitignore`, or a
hardcoded key in a 2019 commit that archaeology surfaces all end up in the
document body. Path-level denylisting (`repo/access.py`) catches the file; this
catches the excerpt.

Patterns are deliberately narrow. A noisy detector here is worse than a quiet
one, because it fires on every document and gets switched off — and then the one
real leak goes through with it. Everything below matches a *credential format*,
not merely a suspicious variable name.
"""

from __future__ import annotations

import re

# Each pattern must match a credential's actual shape. Anything that would fire
# on `api_key = get_api_key()` is excluded by requiring a quoted literal with
# enough entropy-bearing characters to be a real key.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    # The tail must be alphanumeric. The previous form allowed hyphens, which
    # made it fire on ordinary slugs like `sk-metrics-collector-prod-01` — and a
    # detector that cries wolf on service names is a detector someone switches
    # off, taking the real leaks with it.
    # `sk-ant-` keeps hyphens: real keys look like `sk-ant-api03-<long>`, and the
    # prefix is specific enough that a service slug will not collide with it.
    # The looseness only had to leave the bare `sk-` rule below.
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9][A-Za-z0-9-]{18,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b")),
    ("Stripe key", re.compile(r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("PyPI token", re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b")),
    (
        "Azure connection string",
        re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{20,}"),
    ),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+")),
    (
        "assigned credential literal",
        re.compile(
            r"""(?ix)
            # Bare `token` and `secret` were missing, and they are the two most
            # common names in a values.yaml or a .env — the same value was
            # caught under `password` and missed under `token`.
            \b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|
                 client[_-]?secret|password|passwd|token|secret|
                 private[_-]?key|credentials?)\b
            \s*[:=]\s*
            (['"])          # a quoted literal, not a function call
            (?=[^'"]{16,})  # long enough to be a real credential
            (?=[^'"]*[A-Za-z])
            (?=[^'"]*\d)    # and mixed enough not to be a placeholder word
            [^'"\s]+
            \1
            """
        ),
    ),
)

# Obvious non-secrets that the assigned-literal rule would otherwise flag.
_PLACEHOLDERS = re.compile(
    r"(?i)(your[_-]?\w*here|xxx+|placeholder|example|dummy|changeme|<[^>]+>|\$\{[^}]+\}|"
    r"redacted|f4k3|s3cr3t123|test[_-]?key)"
)

REDACTION = "[REDACTED]"


def findings(text: str) -> list[tuple[str, str]]:
    """`(pattern name, matched text)` for every credential-shaped run in `text`."""
    hits: list[tuple[str, str]] = []
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            found = match.group(0)
            if _PLACEHOLDERS.search(found):
                continue
            hits.append((name, found))
    return hits


def contains_secret(text: str) -> bool:
    return bool(findings(text))


def redact(text: str) -> str:
    """Replace credential-shaped runs, preserving everything around them.

    Used on code excerpts before they enter a section body. Redacting is the
    right move rather than dropping the excerpt: the surrounding code is usually
    the thing being explained, and silently omitting it would leave a gap the
    reader cannot see.
    """
    out = text
    for name, found in findings(text):
        out = out.replace(found, REDACTION)
    return out
