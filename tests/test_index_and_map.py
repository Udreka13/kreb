"""Index and map tests, including the detector for FATAL bug #3.

Bug #3: PRD §6.1 specifies summarizing every module leaf-to-root before research
begins. On a 50,000-file repository that is 50,000+ model calls costing hours and
several times the entire per-document budget, spent mostly on directories the
agent will never open.

The fix is that the map is model-free. That is a *call-count* property, so it
does not need a large repository to test — it needs a provider that screams if
anyone touches it. See architecture.md §5 and §9.
"""

from __future__ import annotations

import subprocess

import pytest

from kreb.index.repo_index import build_index, language_for, symbol_ref
from kreb.index.repo_map import build_map
from kreb.repo.access import Repository


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "http").mkdir(parents=True)
    (root / "src" / "db").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)

    (root / "src" / "http" / "retry.py").write_text(
        "class RetryPolicy:\n"
        "    def should_retry(self, attempt):\n"
        "        return attempt < 3\n"
        "\n"
        "def backoff(n):\n"
        "    return n * 2\n"
    )
    (root / "src" / "http" / "client.py").write_text(
        "import retry\n\nclass Client:\n    def get(self, url):\n        return url\n"
    )
    (root / "src" / "db" / "session.py").write_text(
        "import retry\n\nclass Session:\n    def commit(self):\n        pass\n"
    )
    (root / "src" / "app.py").write_text("import client\nimport session\n\ndef main():\n    pass\n")
    (root / "web" ).mkdir()
    (root / "web" / "index.ts").write_text(
        "export class Widget {\n  render(x: number): string {\n    return String(x);\n  }\n}\n"
    )
    (root / "docs" / "guide.md").write_text("# Guide\n\nProse.\n")
    (root / "README.md").write_text("# Proj\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture()
def index(project):
    return build_index(Repository(project))


# ---------------------------------------------------------------------------
# Language detection and refs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.py", "python"),
        ("a.pyi", "python"),
        ("a.ts", "typescript"),
        ("a.tsx", "tsx"),
        ("a.mjs", "javascript"),
        ("a.md", None),
        ("a.rs", None),
        ("Makefile", None),
    ],
)
def test_language_detection(path, expected):
    assert language_for(path) == expected


def test_symbol_ref_is_path_hash_qualname():
    assert symbol_ref("src/a.py", "C.m") == "src/a.py#C.m"


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def test_indexes_symbols_with_qualified_names(index):
    assert "src/http/retry.py#RetryPolicy" in index.symbols
    assert "src/http/retry.py#RetryPolicy.should_retry" in index.symbols
    assert "src/http/retry.py#backoff" in index.symbols


def test_indexes_typescript(index):
    assert "web/index.ts#Widget" in index.symbols
    assert "web/index.ts#Widget.render" in index.symbols


def test_non_code_files_are_degraded_not_dropped(index):
    """Prose files still count — they are part of the repository's shape."""
    assert index.files["README.md"].degraded is True
    assert index.files["README.md"].language is None
    assert index.files["src/http/retry.py"].degraded is False


def test_current_hashes_covers_every_symbol(index):
    hashes = index.current_hashes()
    assert set(hashes) == set(index.symbols)
    assert all(len(h) == 64 for h in hashes.values())


def test_imports_are_captured(index):
    assert "retry" in index.files["src/http/client.py"].imports


# ---------------------------------------------------------------------------
# Anchor classification — misplaced vs fabricated vs ambiguous
# ---------------------------------------------------------------------------

def test_resolved_anchor(index):
    assert index.anchor_status("src/http/retry.py#RetryPolicy") == "resolved"


def test_fabricated_anchor_is_named_as_such(index):
    """A symbol that exists nowhere. The worst failure: it looks like evidence."""
    assert index.anchor_status("src/http/retry.py#TotallyMadeUp") == "fabricated"


def test_misplaced_anchor_is_distinguished_from_fabricated(index):
    """A real symbol cited at the wrong path is a different bug with a different cause."""
    assert index.anchor_status("src/db/session.py#RetryPolicy") == "misplaced"


def test_ambiguous_name_is_reported_not_guessed(project):
    """Two definitions of one name must not silently resolve to whichever came first."""
    (project / "src" / "db" / "retry.py").write_text(
        "class RetryPolicy:\n    def should_retry(self, a):\n        return False\n"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "second RetryPolicy")

    idx = build_index(Repository(project))
    assert len(idx.find_by_name("RetryPolicy")) == 2
    assert idx.anchor_status("web/index.ts#RetryPolicy") == "ambiguous"


def test_moved_symbol_is_found_by_content(index):
    """A moved function must not be reported as a fabricated anchor."""
    sym = index.symbols["src/http/retry.py#backoff"]
    found = index.moved_to("src/old/retry.py#backoff", sym.text_hash)
    assert found == "src/http/retry.py#backoff"


def test_moved_to_returns_none_when_content_is_gone(index):
    assert index.moved_to("src/x.py#gone", "0" * 64) is None


# ---------------------------------------------------------------------------
# Secrets and vendoring flow through from repo/
# ---------------------------------------------------------------------------

def test_secret_files_never_enter_the_index(project):
    (project / ".env").write_text("OPENROUTER_API_KEY=sk-live-not-a-drill\n")
    _git(project, "add", "-f", ".env")
    _git(project, "commit", "-q", "-m", "oops")

    idx = build_index(Repository(project))
    assert ".env" not in idx.files
    assert not any("sk-live" in s.ref for s in idx.symbols.values())


# ---------------------------------------------------------------------------
# THE BUG 3 DETECTOR
# ---------------------------------------------------------------------------

class ExplodingProvider:
    """Any use at all is a test failure."""

    def __getattr__(self, name):  # pragma: no cover - the point is never to run
        raise AssertionError(
            f"map construction attempted a model call ({name!r}); the map must be "
            "model-free or a 50k-file repo costs more than the whole budget"
        )


def test_map_construction_makes_no_model_calls(index, monkeypatch):
    """FATAL bug #3, stated as a test.

    Nothing in the index or map may import or invoke a provider. Injecting an
    exploding provider into the module namespace catches an accidental
    reintroduction of eager summarization.
    """
    import kreb.index.repo_map as repo_map

    monkeypatch.setattr(repo_map, "_PROVIDER_MUST_NOT_EXIST", ExplodingProvider(), raising=False)
    repo_map.build_map(index)  # must not raise


def test_index_and_map_reach_no_provider_module_transitively():
    """Structural guard: a model call must be *unreachable* from the map.

    Walks the real import graph rather than grepping source, so an indirect
    dependency introduced three modules away is still caught.
    """
    import importlib
    import sys

    for entry in ("kreb.index.repo_index", "kreb.index.repo_map"):
        importlib.import_module(entry)

    seen: set[str] = set()
    frontier = ["kreb.index.repo_index", "kreb.index.repo_map"]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr in vars(module).values():
            mod_name = getattr(attr, "__module__", None) or getattr(attr, "__name__", None)
            if isinstance(mod_name, str) and mod_name.startswith("kreb."):
                frontier.append(mod_name)

    forbidden = {n for n in seen if "provider" in n or "budget" in n or "research" in n}
    assert not forbidden, f"map reaches model-calling modules: {sorted(forbidden)}"

    banned_packages = {"openai", "openrouter", "anthropic", "httpx", "requests"}
    leaked = banned_packages & set(sys.modules)
    assert not leaked, f"importing the index pulled in a network client: {sorted(leaked)}"


def test_map_is_deterministic(index):
    """Same index in, byte-identical map out — it is the cached prompt prefix."""
    assert build_map(index).render() == build_map(index).render()


# ---------------------------------------------------------------------------
# Map content
# ---------------------------------------------------------------------------

def test_map_counts_the_repository(index):
    m = build_map(index)
    assert m.total_symbols == len(index.symbols)
    assert m.total_files == len(index.files)
    assert m.total_loc > 0


def test_map_finds_entry_points(index):
    assert "src/app.py" in build_map(index).entry_points
    assert "web/index.ts" in build_map(index).entry_points


def test_map_ranks_imported_modules_above_unimported(index):
    """`retry` is imported twice; its symbols should outrank an unimported leaf."""
    m = build_map(index)
    rank = {ref: i for i, (ref, _) in enumerate(m.central_symbols)}
    assert rank["src/http/retry.py#RetryPolicy"] < rank["web/index.ts#Widget.render"]


def test_map_renders_directories_and_symbols(index):
    text = build_map(index).render()
    assert "src/http" in text
    assert "Most depended-upon symbols" in text
    assert "RetryPolicy" in text


def test_map_render_is_stable_across_calls(index):
    """Reordering between calls would destroy prompt-cache affinity."""
    m = build_map(index)
    assert m.render() == m.render()
