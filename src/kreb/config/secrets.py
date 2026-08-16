"""API key resolution, and the one place that refuses to load one from a file.

PRD §6.5 puts `kreb.toml` at the repository root so that `.kreb/`'s ignore rule
does not swallow it. That decision is right for discoverability and it makes the
config file **the file in the project most likely to be committed** — so it must
be structurally incapable of holding a credential. A key found there is rejected
loudly rather than used, because a tool that accepts it once will have it pushed
to a public remote by the end of the week.

Resolution order is environment, then keyring. Both keep the secret out of the
repository by construction.
"""

from __future__ import annotations

import os

ENV_VARS = ("OPENROUTER_API_KEY", "KREB_API_KEY")

# Keys must never appear under these, at any nesting depth, in kreb.toml.
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "openrouter_api_key",
        "kreb_api_key",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "auth_token",
        "password",
    }
)

KEYRING_SERVICE = "kreb"
KEYRING_USERNAME = "openrouter"


class SecretInConfig(ValueError):
    """A credential was found in a file that belongs in version control."""


class MissingCredential(RuntimeError):
    """No key could be resolved from any permitted source."""


def reject_secrets_in_config(config: dict, *, path: str = "kreb.toml") -> None:
    """Raise if a config mapping contains anything key-shaped, at any depth.

    Checks the *key names*, not the values. A value-based check would have to
    guess what a credential looks like and would miss short or unusual ones;
    a name-based check states a rule the user can follow.
    """
    offenders: list[str] = []

    def walk(node: object, trail: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                if name.lower().replace("-", "_") in FORBIDDEN_KEYS:
                    offenders.append(".".join((*trail, name)))
                walk(value, (*trail, name))
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, (*trail, str(index)))

    walk(config, ())

    if offenders:
        listed = ", ".join(sorted(offenders))
        raise SecretInConfig(
            f"{path} contains credential fields ({listed}). "
            f"{path} lives at the repository root and is meant to be committed. "
            f"Set {ENV_VARS[0]} in the environment, or store the key in the "
            f"system keyring under service '{KEYRING_SERVICE}', and remove these fields."
        )


def from_environment() -> str | None:
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def from_keyring() -> str | None:
    """Read the key from the system keyring, if one is installed.

    `keyring` is an optional dependency: on a headless machine there is often no
    backend, and failing to import it must not stop a run whose key is in the
    environment anyway.
    """
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        return None
    return value.strip() if value else None


def resolve_api_key(*, required: bool = True) -> str | None:
    """The key, from the environment or the keyring, in that order."""
    key = from_environment() or from_keyring()
    if key:
        return key
    if not required:
        return None
    raise MissingCredential(
        "No API key found. Set "
        + " or ".join(ENV_VARS)
        + f", or store one in the system keyring under service '{KEYRING_SERVICE}' "
        f"and username '{KEYRING_USERNAME}'. It must not be placed in kreb.toml."
    )


def redact(text: str, key: str | None) -> str:
    """Remove a known key from text on its way to a log or an error."""
    if not key or len(key) < 8:
        return text
    return text.replace(key, "[REDACTED]")
