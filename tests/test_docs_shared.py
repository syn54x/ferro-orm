"""The snippet-region reader and the connect redirect the docs harness shares."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import _docs_harness, _docs_sql
from tests._docs_shared import (
    SnippetRegion,
    redirected_url,
    snippet_regions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPT = """\
from ferro import Model
# --8<-- [start:models]
class Author(Model):
    # --8<-- [start:field]
    name: str
    # --8<-- [end:field]
# --8<-- [end:models]
print("done")
"""


def test_a_region_is_its_marker_lines_and_the_lines_between() -> None:
    assert snippet_regions(SCRIPT) == {
        "models": SnippetRegion(
            start=2,
            end=7,
            body=(
                "class Author(Model):\n"
                "    # --8<-- [start:field]\n"
                "    name: str\n"
                "    # --8<-- [end:field]\n"
            ),
        ),
        "field": SnippetRegion(start=4, end=6, body="    name: str\n"),
    }


def test_markdown_regions_read_the_same_way() -> None:
    text = "intro\n<!-- --8<-- [start:intro] -->\nHello.\n<!-- --8<-- [end:intro] -->\n"
    assert snippet_regions(text) == {"intro": SnippetRegion(2, 4, "Hello.\n")}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "# --8<-- [start:a]\n# --8<-- [end:a]\n# --8<-- [start:a]\n# --8<-- [end:a]\n",
            "ex.py:3: the snippet region 'a' starts a second time",
        ),
        (
            "x\n# --8<-- [end:a]\n",
            "ex.py:2: the snippet region 'a' ends without starting",
        ),
        ("# --8<-- [start:a]\nx\n", "ex.py:1: the snippet region 'a' never ends"),
    ],
    ids=["twice", "end-only", "unclosed"],
)
def test_a_malformed_region_is_refused_naming_its_line(text: str, message: str) -> None:
    with pytest.raises(ValueError) as raised:
        snippet_regions(text, where="ex.py")
    assert str(raised.value).startswith(message), str(raised.value)


def _region_files() -> list[Path]:
    scripts = [
        path
        for path in (REPO_ROOT / "docs" / "examples").glob("*.py")
        if not path.name.startswith("_")
    ]
    markdown = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        *(REPO_ROOT / "docs" / "includes").glob("*.md"),
    ]
    return sorted([*scripts, *markdown])


@pytest.mark.parametrize("path", _region_files(), ids=lambda p: p.name)
def test_every_file_with_regions_reads_cleanly(path: Path) -> None:
    snippet_regions(path.read_text(encoding="utf-8"), where=str(path))


def test_the_four_region_readers_are_one() -> None:
    """The capture, the include reader, the README check and the migrations
    examples' helper all read regions with ``snippet_regions``."""
    import importlib
    import sys

    from tests import test_docs_examples

    assert _docs_harness.snippet_regions is snippet_regions
    assert not hasattr(_docs_sql, "snippet_ranges")
    assert not hasattr(_docs_harness, "_section")
    assert not hasattr(test_docs_examples, "_MODELS_SECTION")
    sys.path.insert(0, str(REPO_ROOT / "docs" / "examples"))
    try:
        helper = importlib.import_module("_migrations_project")
    finally:
        sys.path.remove(str(REPO_ROOT / "docs" / "examples"))
    assert helper.snippet_regions is snippet_regions


PG = "postgresql://postgres:password@localhost:5432/postgres?ferro_search_path=ferro_ab12"


@pytest.mark.parametrize(
    ("named", "reached"),
    [
        ("postgresql://localhost/app", PG),
        (
            "postgresql://user:password@localhost:5432/app?ferro_search_path=tenant_a",
            "postgresql://postgres:password@localhost:5432/postgres"
            "?ferro_search_path=tenant_a",
        ),
        (
            "postgresql://user:password@localhost:5432/app?sslmode=require",
            "postgresql://postgres:password@localhost:5432/postgres"
            "?ferro_search_path=ferro_ab12&sslmode=require",
        ),
        ("sqlite:dev.db?mode=rwc", PG),
        ("sqlite::memory:", PG),
    ],
    ids=["no-options", "search-path", "other-option", "sqlite-options", "sqlite"],
)
def test_a_redirect_keeps_the_postgres_options_the_block_names(
    named: str, reached: str
) -> None:
    assert redirected_url(named, PG) == reached


def test_the_capture_and_the_prelude_runner_install_the_one_redirect() -> None:
    """Neither patches ``ferro.connect`` itself."""
    for path in (
        REPO_ROOT / "tests" / "_docs_sql.py",
        REPO_ROOT / "docs" / "examples" / "_preludes" / "__init__.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "ferro.connect =" not in source, path
        assert "point_connect_at(" in source, path
